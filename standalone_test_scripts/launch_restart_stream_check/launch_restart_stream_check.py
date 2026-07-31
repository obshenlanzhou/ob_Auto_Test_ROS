#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from _test_protocol import (
    EventWriter,
    artifact_list,
    atomic_write_json,
    collect_test_environment,
    contract_result,
    iso_now,
    namespace_request,
    parse_camera,
    test_environment_markdown,
)

DEFAULT_CAMERA_LAUNCH = {
    ("2", "gemini_301"): "gemini_301_series.launch.py",
    ("2", "gemini_330"): "gemini_330_series.launch.py",
    ("1", "gemini_301"): "gemini_301_series.launch",
    ("1", "gemini_330"): "gemini_330_series.launch",
}
ENV_READY_VAR = "LAUNCH_RESTART_STREAM_CHECK_ENV_READY"
INTERRUPTED = False
TOOL_VERSION = "1.0"
TEST_ID = "launch_restart_stream_check"
DEFAULT_STRESS_LAUNCH_ARGS = {
    "enable_heartbeat": "true",
    "enable_firmware_log": "true",
}


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handle_sigint(signum, frame) -> None:
    global INTERRUPTED
    INTERRUPTED = True
    raise KeyboardInterrupt


def parse_duration(value: Any, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    raw = str(value).strip().lower()
    multiplier = 1.0
    if raw.endswith("s"):
        raw = raw[:-1]
    elif raw.endswith("m"):
        raw = raw[:-1]
        multiplier = 60.0
    elif raw.endswith("h"):
        raw = raw[:-1]
        multiplier = 3600.0
    duration = float(raw) * multiplier
    if duration <= 0.0:
        raise ValueError("duration values must be > 0")
    return duration


def parse_launch_arg(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if ":=" in text:
        key, value = text.split(":=", 1)
    elif "=" in text:
        key, value = text.split("=", 1)
    else:
        raise ValueError(f"launch arg must be KEY=VALUE or KEY:=VALUE: {raw}")
    key = key.strip()
    if not key:
        raise ValueError(f"launch arg key is empty: {raw}")
    return key, value.strip()


def merge_launch_arg_overrides(
    launch_args: Dict[str, str],
    raw_launch_args: List[str],
) -> Dict[str, str]:
    merged = dict(launch_args)
    for raw_arg in raw_launch_args:
        key, value = parse_launch_arg(raw_arg)
        merged[key] = value
    return merged


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_image_type(type_name: str) -> bool:
    return type_name in {"sensor_msgs/msg/Image", "sensor_msgs/Image"}


def _is_compressed_image_type(type_name: str) -> bool:
    return type_name in {"sensor_msgs/msg/CompressedImage", "sensor_msgs/CompressedImage"}


def _image_topic_kind(type_name: str) -> Optional[str]:
    if _is_image_type(type_name):
        return "raw"
    if _is_compressed_image_type(type_name):
        return "compressed"
    return None


class StatusLogger:
    def __init__(self, events: Optional[EventWriter] = None) -> None:
        self.events = events

    def __call__(self, message: str, *, event: str = "log", **fields: Any) -> None:
        line = f"[{timestamp()}] {message}"
        print(line, flush=True)
        if self.events is not None:
            self.events.emit(event, message, **fields)


def capture_sourced_env(ros_setup: str, driver_setup: str, ros_version: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["ROS_VERSION"] = ros_version
    command_parts = []
    for setup_file in (ros_setup, driver_setup):
        setup_file = str(setup_file or "").strip()
        if not setup_file:
            continue
        setup_path = Path(setup_file).expanduser()
        if not setup_path.is_file():
            raise FileNotFoundError(f"setup file not found: {setup_path}")
        command_parts.append(f"source {shlex.quote(str(setup_path))} >/dev/null 2>&1")
    if not command_parts:
        return env
    command = " && ".join(command_parts) + " && env -0"
    raw_output = subprocess.check_output(["bash", "-lc", command], env=env)
    sourced_env: Dict[str, str] = {}
    for chunk in raw_output.split(b"\0"):
        if not chunk or b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        sourced_env[key.decode("utf-8")] = value.decode("utf-8")
    sourced_env["ROS_VERSION"] = ros_version
    sourced_env["PYTHONUNBUFFERED"] = "1"
    return sourced_env


def prepare_runtime_env(args) -> Dict[str, str]:
    if os.environ.get(ENV_READY_VAR) == "1":
        runtime_env = dict(os.environ)
        runtime_env["ROS_VERSION"] = args.ros_version
        runtime_env["PYTHONUNBUFFERED"] = "1"
        return runtime_env

    runtime_env = capture_sourced_env(args.ros_setup, args.driver_setup, args.ros_version)
    runtime_env[ENV_READY_VAR] = "1"
    if args.ros_setup or args.driver_setup:
        executable = sys.executable or "python3"
        os.execvpe(executable, [executable, *sys.argv], runtime_env)
    return runtime_env


def apply_python_paths(runtime_env: Dict[str, str]) -> None:
    os.environ.update(runtime_env)
    for item in reversed(runtime_env.get("PYTHONPATH", "").split(os.pathsep)):
        if item and item not in sys.path:
            sys.path.insert(0, item)


def launch_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_launch_command(
    *,
    ros_version: str,
    launch_package: str,
    launch_file: str,
    launch_args: Dict[str, str],
) -> List[str]:
    launch_path = Path(launch_file).expanduser()
    if launch_path.is_absolute() or launch_path.parent != Path("."):
        command = (
            ["roslaunch", str(launch_path)]
            if ros_version == "1"
            else ["ros2", "launch", str(launch_path)]
        )
    else:
        command = (
            ["roslaunch", launch_package, launch_file]
            if ros_version == "1"
            else ["ros2", "launch", launch_package, launch_file]
        )
    for key, value in sorted(launch_args.items()):
        if value is None or value == "":
            continue
        command.append(f"{key}:={launch_value(value)}")
    return command


class LaunchSession:
    def __init__(
        self,
        *,
        command: List[str],
        work_dir: Path,
        env: Dict[str, str],
        log_path: Path,
        emit: StatusLogger,
    ) -> None:
        self.command = command
        self.work_dir = work_dir
        self.env = env
        self.log_path = log_path
        self.emit = emit
        self.process: Optional[subprocess.Popen[str]] = None
        self._log_handle = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("launch is already running")
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self._log_handle.write("$ " + " ".join(shlex.quote(item) for item in self.command) + "\n\n")
        self._log_handle.flush()
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.work_dir,
                env=self.env,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        except Exception:
            self._close_log()
            raise

    def poll(self) -> Optional[int]:
        if self.process is None:
            return None
        return self.process.poll()

    def assert_running(self) -> None:
        code = self.poll()
        if code is not None:
            raise RuntimeError(f"launch process exited unexpectedly with code {code}")

    def stop(self, timeout: float = 10.0) -> None:
        if self.process is None:
            self._close_log()
            return
        if self.process.poll() is not None:
            self._close_log()
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.emit("launch did not stop after SIGINT, sending SIGTERM")
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.emit("launch did not stop after SIGTERM, sending SIGKILL")
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5.0)
        self._close_log()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


class RosImageHarness:
    def __init__(self, ros_version: str, node_name: str, queue_size: int) -> None:
        self.ros_version = ros_version
        self.node_name = node_name
        self.queue_size = queue_size
        self._rclpy = None
        self._rospy = None
        self.node = None
        self.subscriptions = []
        self.message_types = {}

    def __enter__(self) -> "RosImageHarness":
        if self.ros_version == "2":
            try:
                import rclpy
                from sensor_msgs.msg import CompressedImage, Image
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS2 Python modules. Source ROS2 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rclpy.init(args=None)
            self._rclpy = rclpy
            self.node = rclpy.create_node(self.node_name)
            self.message_types = {"raw": Image, "compressed": CompressedImage}
        else:
            try:
                import rospy
                from sensor_msgs.msg import CompressedImage, Image
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS1 Python modules. Source ROS1 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rospy.init_node(self.node_name, anonymous=True, disable_signals=True)
            self._rospy = rospy
            self.message_types = {"raw": Image, "compressed": CompressedImage}
        return self

    def get_topic_names_and_types(self) -> Dict[str, List[str]]:
        if self.ros_version == "2":
            return {
                topic_name: list(type_names)
                for topic_name, type_names in self.node.get_topic_names_and_types()
            }
        return {
            topic_name: [type_name]
            for topic_name, type_name in self._rospy.get_published_topics(namespace="/")
        }

    def resolve_image_topic_kind(self, topic: str) -> str:
        topic_types = self.get_topic_names_and_types()
        candidate_names = [topic]
        if not topic.startswith("/"):
            candidate_names.append(f"/{topic}")
        for candidate_name in candidate_names:
            for type_name in topic_types.get(candidate_name, []):
                kind = _image_topic_kind(type_name)
                if kind:
                    return kind
        if topic.rstrip("/").endswith("/compressed"):
            return "compressed"
        return "raw"

    def create_image_subscription(self, topic: str, callback, topic_kind: Optional[str] = None) -> None:
        topic_kind = topic_kind or self.resolve_image_topic_kind(topic)
        message_type = self.message_types[topic_kind]
        if self.ros_version == "2":
            sub = self.node.create_subscription(message_type, topic, callback, self.queue_size)
        else:
            sub = self._rospy.Subscriber(topic, message_type, callback, queue_size=self.queue_size)
        self.subscriptions.append(sub)
        return sub

    def destroy_subscription(self, subscription) -> None:
        if self.ros_version == "2":
            self.node.destroy_subscription(subscription)
        else:
            subscription.unregister()
        if subscription in self.subscriptions:
            self.subscriptions.remove(subscription)

    def spin_once(self, timeout_sec: float) -> None:
        if self.ros_version == "2":
            self._rclpy.spin_once(self.node, timeout_sec=timeout_sec)
        else:
            time.sleep(timeout_sec)

    def list_image_topics(self) -> List[str]:
        topics: List[str] = []
        for topic_name, type_names in self.get_topic_names_and_types().items():
            if any(_is_image_type(type_name) for type_name in type_names):
                topics.append(topic_name)
        return sorted(set(topics))

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ros_version == "2":
            for subscription in list(self.subscriptions):
                try:
                    self.node.destroy_subscription(subscription)
                except Exception:
                    pass
            try:
                self.node.destroy_node()
            except Exception:
                pass
            try:
                if self._rclpy.ok():
                    self._rclpy.shutdown()
            except Exception:
                pass
        else:
            for subscription in list(self.subscriptions):
                try:
                    subscription.unregister()
                except Exception:
                    pass


class StableImageMonitor:
    def __init__(self, harness: RosImageHarness, topics: List[str], emit: StatusLogger) -> None:
        self.harness = harness
        self.topics = topics
        self.emit = emit
        self.state: Dict[str, Dict[str, Any]] = {}
        self.subscriptions = []
        for topic in topics:
            topic_kind = self.harness.resolve_image_topic_kind(topic)
            self.state[topic] = {
                "topic_kind": topic_kind,
                "message_count": 0,
                "first_message_at": None,
                "last_message_at": None,
                "last_width": 0,
                "last_height": 0,
                "last_data_size": 0,
            }
            self.subscriptions.append(
                self.harness.create_image_subscription(
                    topic,
                    lambda msg, topic_name=topic: self._on_message(topic_name, msg),
                    topic_kind=topic_kind,
                )
            )

    def _on_message(self, topic_name: str, message: Any) -> None:
        now = time.monotonic()
        width = int(getattr(message, "width", 0) or 0)
        height = int(getattr(message, "height", 0) or 0)
        data_size = len(getattr(message, "data", b"") or b"")
        item = self.state[topic_name]
        item["message_count"] += 1
        item["last_message_at"] = now
        item["last_width"] = width
        item["last_height"] = height
        item["last_data_size"] = data_size
        has_valid_payload = (
            data_size > 0
            if item["topic_kind"] == "compressed"
            else width > 0 and height > 0
        )
        if item["first_message_at"] is None and has_valid_payload:
            item["first_message_at"] = now

    def snapshot(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        rows = []
        for topic_name, item in self.state.items():
            first_message_at = item["first_message_at"]
            last_message_at = item["last_message_at"]
            rows.append(
                {
                    "name": topic_name,
                    "topic_kind": item["topic_kind"],
                    "message_count": item["message_count"],
                    "width": item["last_width"],
                    "height": item["last_height"],
                    "data_size": item["last_data_size"],
                    "seconds_since_last_message": (
                        now - last_message_at if last_message_at is not None else None
                    ),
                    "stable_seconds": (
                        now - first_message_at if first_message_at is not None else 0.0
                    ),
                }
            )
        return rows

    def stable_for(self, stable_seconds: float, max_gap_seconds: float) -> bool:
        now = time.monotonic()
        for item in self.state.values():
            first_message_at = item["first_message_at"]
            last_message_at = item["last_message_at"]
            if first_message_at is None or last_message_at is None:
                return False
            if now - last_message_at > max_gap_seconds:
                item["first_message_at"] = None
                return False
            if now - first_message_at < stable_seconds:
                return False
        return True

    def all_streams_detected(self) -> bool:
        return all(item["first_message_at"] is not None for item in self.state.values())

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


def wait_for_stable_streams(
    *,
    session: LaunchSession,
    harness: RosImageHarness,
    topics: List[str],
    stable_seconds: float,
    timeout: float,
    max_gap_seconds: float,
    emit: StatusLogger,
) -> tuple[bool, List[Dict[str, Any]], str]:
    monitor = StableImageMonitor(harness, topics, emit)
    deadline = time.monotonic() + timeout
    streams_detected_logged = False
    try:
        while time.monotonic() < deadline:
            session.assert_running()
            harness.spin_once(0.1)
            if not streams_detected_logged and monitor.all_streams_detected():
                emit(f"streams detected, checking {stable_seconds:.1f}s stability")
                streams_detected_logged = True
            if monitor.stable_for(stable_seconds, max_gap_seconds):
                return True, monitor.snapshot(), "image streams are stable"
        return False, monitor.snapshot(), f"image streams were not stable within {timeout:.1f}s"
    finally:
        monitor.close()


def discover_image_topics(
    *,
    session: LaunchSession,
    harness: RosImageHarness,
    timeout: float,
    emit: StatusLogger,
) -> List[str]:
    deadline = time.monotonic() + timeout
    last_topics: List[str] = []
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.2)
        last_topics = harness.list_image_topics()
    if last_topics:
        return last_topics
    raise RuntimeError(f"no sensor_msgs/Image topics discovered within {timeout:.1f}s")


def expand_camera_topic(topic: str, camera_name: str) -> str:
    return topic.replace("{camera}", camera_name).replace("${camera}", camera_name)


def build_summary(result: Dict[str, Any]) -> str:
    command = result.get("command", [])
    command_text = " ".join(shlex.quote(str(item)) for item in command) if command else ""
    topics = result.get("image_topics", [])
    lines = [
        "# Launch Restart Stream Check",
        "",
        *test_environment_markdown(result.get("environment", {})),
        "## Command",
        "",
        "```bash",
        command_text,
        "```",
        "",
        "## Result",
        "",
        f"- Status: {result.get('status', '')}",
        f"- Tool version: {result.get('tool_version', '')}",
        f"- Successful restarts: {result.get('successful_restarts', 0)}",
        f"- Launch attempts: {result.get('launch_attempts', 0)}",
        f"- Elapsed seconds: {float(result.get('elapsed_seconds', 0.0) or 0.0):.1f}",
        "",
        "## Monitored Streams",
        "",
    ]
    if topics:
        lines.extend(f"- {topic}" for topic in topics)
    else:
        lines.append("- None")
    if result.get("manual_confirmation_message"):
        lines.extend(
            [
                "",
                "## Manual Confirmation",
                "",
                str(result.get("manual_confirmation_message", "")),
            ]
        )
    if result.get("error"):
        lines.extend(["", "## Error", "", str(result.get("error", ""))])
    return "\n".join(lines) + "\n"


def select_launch_file(args) -> str:
    if args.launch_file:
        return args.launch_file
    camera_model = str(args.camera_model or "").strip()
    launch_file = DEFAULT_CAMERA_LAUNCH.get((args.ros_version, camera_model))
    if launch_file:
        return launch_file
    raise ValueError("--launch-file is required unless --camera-model has a built-in default")


def run(args) -> int:
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    runtime_env = prepare_runtime_env(args)
    apply_python_paths(runtime_env)
    environment = collect_test_environment(args)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_restart_stream")
    run_started_at = iso_now()
    default_results_dir = Path(__file__).resolve().parent / "results" / run_id
    results_dir = ensure_dir(Path(args.results_dir).resolve() if args.results_dir else default_results_dir)
    events = EventWriter(results_dir / "events.jsonl")
    emit = StatusLogger(events)

    launch_file = select_launch_file(args)
    if len(args.camera) > 1:
        raise ValueError("launch restart accepts at most one --camera")
    camera = parse_camera(args.camera[0]) if args.camera else parse_camera("name=camera")
    template_camera_name = camera["name"]
    explicit_topics = [
        expand_camera_topic(item.strip(), template_camera_name)
        for item in args.image_topic
        if item.strip()
    ]
    auto_discover_topics = not explicit_topics

    launch_args: Dict[str, str] = dict(DEFAULT_STRESS_LAUNCH_ARGS)
    if camera["name"]:
        launch_args["camera_name"] = camera["name"]
    if camera["serial_number"]:
        launch_args["serial_number"] = camera["serial_number"]
    if camera["usb_port"]:
        launch_args["usb_port"] = camera["usb_port"]
    if camera["device_ip"]:
        launch_args["net_device_ip"] = camera["device_ip"]
    if camera["device_port"]:
        launch_args["net_device_port"] = camera["device_port"]
    if camera["config_file_path"]:
        launch_args["config_file_path"] = camera["config_file_path"]
    launch_args = merge_launch_arg_overrides(launch_args, args.launch_arg)
    launch_args["log_level"] = args.sdk_log_level
    launch_args["log_file_name"] = f"{template_camera_name}.log"

    duration_seconds = parse_duration(args.duration, 300.0)
    stable_seconds = parse_duration(args.stable_seconds, 5.0)
    stream_timeout = parse_duration(args.stream_timeout, 20.0)
    topic_discovery_timeout = parse_duration(args.topic_discovery_timeout, 15.0)
    max_gap_seconds = parse_duration(args.max_gap_seconds, 1.5)
    restart_delay = float(args.restart_delay)
    run_count = args.run_count
    if run_count is not None and run_count <= 0:
        raise ValueError("--run-count must be > 0")
    deadline = time.monotonic() + duration_seconds

    command = build_launch_command(
        ros_version=args.ros_version,
        launch_package=args.launch_package,
        launch_file=launch_file,
        launch_args=launch_args,
    )

    result: Dict[str, Any] = {
        "status": "passed",
        "tool_version": TOOL_VERSION,
        "environment": environment,
        "ros_version": args.ros_version,
        "command": command,
        "launch_file": launch_file,
        "launch_package": args.launch_package,
        "launch_args": launch_args,
        "camera": camera,
        "topic_mode": "auto" if auto_discover_topics else "manual",
        "image_topics": explicit_topics,
        "discovered_image_topics": [],
        "duration_seconds": duration_seconds,
        "stable_seconds_required": stable_seconds,
        "stream_timeout_seconds": stream_timeout,
        "max_gap_seconds": max_gap_seconds,
        "elapsed_seconds": 0.0,
        "successful_restarts": 0,
        "launch_attempts": 0,
        "attempts": [],
    }

    emit("test started", event="phase", phase="starting")
    emit(f"tool version: {TOOL_VERSION}")
    emit(f"results dir: {results_dir}")
    emit("launch command: " + " ".join(shlex.quote(item) for item in command))
    emit(f"planned duration: {duration_seconds:.1f}s")
    if auto_discover_topics:
        emit("monitor topics: auto discover on first launch")
    else:
        emit(
            f"monitor topics: {', '.join(explicit_topics)} "
            f"(stable {stable_seconds:.1f}s, timeout {stream_timeout:.1f}s per launch)"
        )

    attempt_index = 0
    monitored_topics = list(explicit_topics)
    test_start_monotonic = time.monotonic()
    active_session: Optional[LaunchSession] = None
    current_attempt: Optional[Dict[str, Any]] = None
    keep_launch_running = False
    try:
        with RosImageHarness(args.ros_version, "launch_restart_stream_check", args.queue_size) as harness:
            while time.monotonic() < deadline:
                if run_count is not None and attempt_index >= run_count:
                    break
                attempt_index += 1
                attempt_dir = ensure_dir(results_dir / "logs" / f"test_{attempt_index:04d}")
                attempt_env = dict(runtime_env)
                attempt_env["ORBBEC_LOG_DIR"] = str(
                    ensure_dir(attempt_dir / "sdk")
                )
                session = LaunchSession(
                    command=command,
                    work_dir=results_dir,
                    env=attempt_env,
                    log_path=attempt_dir / f"{template_camera_name}.launch.log",
                    emit=emit,
                )
                active_session = session
                attempt = {
                    "attempt": attempt_index,
                    "status": "running",
                    "message": "",
                    "topics": [],
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "ended_at": "",
                    "stable_seconds": 0.0,
                    "launch_log": str(attempt_dir / f"{template_camera_name}.launch.log"),
                }
                current_attempt = attempt
                result["attempts"].append(attempt)
                result["launch_attempts"] = attempt_index

                emit(
                    f"attempt {attempt_index}: start launch",
                    event="progress",
                    current=attempt_index,
                    total=run_count,
                    phase="launching",
                )
                session.start()
                if auto_discover_topics and not monitored_topics:
                    monitored_topics = discover_image_topics(
                        session=session,
                        harness=harness,
                        timeout=topic_discovery_timeout,
                        emit=emit,
                    )
                    attempt["discovered_topics"] = monitored_topics
                    result["discovered_image_topics"] = list(monitored_topics)
                    result["image_topics"] = list(monitored_topics)
                    emit(
                        f"monitor topics: {', '.join(monitored_topics)} "
                        f"(stable {stable_seconds:.1f}s, timeout {stream_timeout:.1f}s per launch)"
                    )
                elif auto_discover_topics:
                    pass
                ok, snapshot, message = wait_for_stable_streams(
                    session=session,
                    harness=harness,
                    topics=monitored_topics,
                    stable_seconds=stable_seconds,
                    timeout=stream_timeout,
                    max_gap_seconds=max_gap_seconds,
                    emit=emit,
                )
                attempt["topics"] = snapshot
                attempt["message"] = message
                attempt["stable_seconds"] = min(
                    (item.get("stable_seconds", 0.0) or 0.0 for item in snapshot),
                    default=0.0,
                )
                if not ok:
                    attempt["status"] = "failed"
                    result["status"] = "failed"
                    result["manual_confirmation_required"] = True
                    result["manual_confirmation_message"] = (
                        f"attempt {attempt_index}: {message}; launch was kept running "
                        "until manual stream check finished"
                    )
                    keep_launch_running = True
                    emit(
                        f"attempt {attempt_index}: streams not stable within "
                        f"{stream_timeout:.1f}s, launch kept running for manual check"
                    )
                    emit(
                        "please manually check whether image streams are publishing, "
                        "press Ctrl+C to stop launch and finish"
                    )
                    while session.poll() is None:
                        time.sleep(1.0)
                    break

                attempt["status"] = "passed"
                attempt["ended_at"] = datetime.now().isoformat(timespec="seconds")
                result["successful_restarts"] += 1
                emit(
                    f"attempt {attempt_index}: passed, stop launch",
                    event="progress",
                    current=attempt_index,
                    total=run_count,
                    phase="completed-cycle",
                )
                session.stop()
                active_session = None
                current_attempt = None

                if time.monotonic() >= deadline:
                    break
                time.sleep(min(restart_delay, max(deadline - time.monotonic(), 0.0)))
    except KeyboardInterrupt:
        if result.get("manual_confirmation_required"):
            emit("manual check finished by user")
            keep_launch_running = False
        else:
            result["status"] = "interrupted"
            emit("test interrupted by user")
        if current_attempt is not None:
            if not result.get("manual_confirmation_required"):
                current_attempt["status"] = "interrupted"
                current_attempt["message"] = "interrupted by user"
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            result["status"] = "interrupted"
            if current_attempt is not None:
                current_attempt["status"] = "interrupted"
                current_attempt["message"] = "interrupted by user"
            emit("test interrupted by user")
        else:
            result["status"] = "failed"
            result["error"] = str(exc)
            if current_attempt is not None:
                current_attempt["status"] = "failed"
                current_attempt["message"] = str(exc)
            emit(f"test failed: {exc}")
    finally:
        if active_session is not None and not keep_launch_running:
            emit("stop launch")
            active_session.stop()
        result["elapsed_seconds"] = time.monotonic() - test_start_monotonic
        for attempt in result.get("attempts", []):
            if not attempt.get("ended_at"):
                attempt["ended_at"] = datetime.now().isoformat(timespec="seconds")
        summary_text = build_summary(result)
        (results_dir / "summary.md").write_text(summary_text, encoding="utf-8")
        ended_at = iso_now()
        emit(
            f"test finished with status {result['status']}",
            event="completed",
            status=result["status"],
        )
        payload = contract_result(
            test_id=TEST_ID,
            run_id=run_id,
            started_at=run_started_at,
            ended_at=ended_at,
            request=namespace_request(args),
            details=result,
            summary={
                "successful_restarts": result["successful_restarts"],
                "launch_attempts": result["launch_attempts"],
            },
            artifacts=artifact_list(results_dir),
        )
        atomic_write_json(results_dir / "result.json", payload)
        signal.signal(signal.SIGINT, previous_sigint_handler)

    if result["status"] == "passed":
        emit(f"test finished successfully, successful restarts={result['successful_restarts']}")
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Repeatedly restart a ROS launch file and verify image streams recover.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 ./launch_restart_stream_check/launch_restart_stream_check.py --ros-version 2 "
            "--ros-setup /opt/ros/humble/setup.bash "
            "--driver-setup /path/to/camera_ws/install/setup.bash "
            "--camera-model gemini_301 --duration 1h\n\n"
            "  python3 ./launch_restart_stream_check/launch_restart_stream_check.py --launch-file /path/to/multi_camera.launch.py "
            "--duration 1h\n\n"
            "  python3 ./launch_restart_stream_check/launch_restart_stream_check.py --launch-file /path/to/test.launch.py "
            "--image-topic /camera/color/image_raw --stable-seconds 5\n"
        ),
    )
    parser.add_argument("--ros-version", choices=("1", "2"), default=os.environ.get("ROS_VERSION", "2"))
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument("--driver-setup", default=os.environ.get("ORBBEC_CAMERA_SETUP", ""))
    parser.add_argument("--camera-model", default="", help="Optional built-in default launch selector, e.g. gemini_301")
    parser.add_argument("--launch-package", default="orbbec_camera")
    parser.add_argument("--launch-file", default="", help="Launch filename or absolute/relative launch path")
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra launch arg, KEY=VALUE or KEY:=VALUE")
    parser.add_argument(
        "--sdk-log-level",
        choices=("debug", "info", "warn", "error", "fatal", "none"),
        default="debug",
        help="Orbbec SDK log level (default: debug)",
    )
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        help=(
            "Camera launch arguments as comma-separated KEY=VALUE fields. "
            "Supported keys: name, serial-number, usb-port, device-ip, "
            "device-port, config-file-path."
        ),
    )
    parser.add_argument(
        "--image-topic",
        action="append",
        default=[],
        help=(
            "Image or compressed image topic to monitor; can repeat. If omitted, sensor_msgs/Image "
            "topics are auto discovered during the first launch attempt and then reused."
        ),
    )
    parser.add_argument(
        "--topic-discovery-timeout",
        default="15",
        help="Max wait time for auto image topic discovery during the first launch attempt",
    )
    parser.add_argument("--duration", default="300", help="Total test duration, supports seconds, 15m, 2h")
    parser.add_argument(
        "--run-count",
        type=int,
        default=None,
        help="Maximum completed restart cycles; duration still applies when both are set",
    )
    parser.add_argument("--stable-seconds", default="5", help="Required continuous stable image duration per launch")
    parser.add_argument("--stream-timeout", default="20", help="Max wait time for stable stream per launch")
    parser.add_argument("--max-gap-seconds", default="1.5", help="Max allowed receive gap between images")
    parser.add_argument("--restart-delay", default="2", help="Delay seconds between stop and next start")
    parser.add_argument("--queue-size", type=int, default=10)
    parser.add_argument("--results-dir", default="")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(TOOL_VERSION),
    )
    return parser.parse_args()


def main() -> None:
    try:
        sys.exit(run(parse_args()))
    except KeyboardInterrupt:
        print(f"[{timestamp()}] test interrupted by user", flush=True)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            print(f"[{timestamp()}] test interrupted by user", flush=True)
            sys.exit(130)
        print(f"[{timestamp()}] error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
