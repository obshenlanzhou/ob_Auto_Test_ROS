#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from _test_protocol import (
    EventWriter,
    artifact_list,
    atomic_write_json,
    collect_test_environment,
    contract_result,
    install_terminal_log,
    iso_now,
    namespace_request,
    parse_camera,
    test_environment_markdown,
)
from _sensor_artifacts import (
    SensorCaptureMonitor,
    SensorArtifactPathSequence,
    capture_sensor_artifacts,
    discover_sensor_topics,
    expand_topic_templates,
)

DEFAULT_CAMERA_LAUNCH = {
    ("2", "gemini_301"): "gemini_301_series.launch.py",
    ("2", "gemini_330"): "gemini_330_series.launch.py",
    ("1", "gemini_301"): "gemini_301_series.launch",
    ("1", "gemini_330"): "gemini_330_series.launch",
}
ENV_READY_VAR = "LAUNCH_RESTART_STREAM_CHECK_ENV_READY"
INTERRUPTED = False
TOOL_VERSION = "2.2.2"
TEST_ID = "launch_restart_stream_check"
POINT_CLOUD_RESUBSCRIBE_AFTER_SECONDS = 2.0
IMAGE_RESUBSCRIBE_AFTER_PEER_SECONDS = 2.0
IMAGE_RESUBSCRIBE_STARTUP_GRACE_SECONDS = 10.0
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


def emit_failed_attempt(
    emit: Any,
    message: str,
    *,
    attempt_index: int,
    run_count: Optional[int],
) -> None:
    """Emit the terminal event used by the UI to count a failed attempt."""
    emit(
        message,
        event="failure",
        status="failed",
        current=attempt_index,
        total=run_count,
        attempt=attempt_index,
        phase="failed-cycle",
    )


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

    def stop(self, timeout: float = 20.0) -> None:
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
        self._sensor_qos = None
        self.subscriptions = []
        self.message_types = {}

    def __enter__(self) -> "RosImageHarness":
        if self.ros_version == "2":
            try:
                import rclpy
                from rclpy.qos import qos_profile_sensor_data
                from sensor_msgs.msg import CompressedImage, Image, Imu, PointCloud2
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS2 Python modules. Source ROS2 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rclpy.init(args=None)
            self._rclpy = rclpy
            self._sensor_qos = qos_profile_sensor_data
            self.node = rclpy.create_node(self.node_name)
            self.message_types = {
                "raw": Image,
                "compressed": CompressedImage,
                "point_cloud": PointCloud2,
                "imu": Imu,
            }
        else:
            try:
                import rospy
                from sensor_msgs.msg import CompressedImage, Image, Imu, PointCloud2
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS1 Python modules. Source ROS1 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rospy.init_node(self.node_name, anonymous=True, disable_signals=True)
            self._rospy = rospy
            self.message_types = {
                "raw": Image,
                "compressed": CompressedImage,
                "point_cloud": PointCloud2,
                "imu": Imu,
            }
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

    def create_sensor_subscription(self, topic: str, kind: str, callback):
        message_type = self.message_types[kind]
        if self.ros_version == "2":
            sub = self.node.create_subscription(
                message_type, topic, callback, self._sensor_qos
            )
        else:
            sub = self._rospy.Subscriber(
                topic, message_type, callback, queue_size=self.queue_size
            )
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


def drain_inactive_subscriptions(harness: RosImageHarness, topic_count: int) -> None:
    if getattr(harness, "ros_version", "2") != "2":
        return
    queue_depth = max(int(getattr(harness, "queue_size", 10) or 10), 1)
    for _ in range(max(32, queue_depth * max(topic_count, 1) * 2)):
        harness.spin_once(0.0)


def wait_for_stable_streams(
    *,
    session: LaunchSession,
    harness: RosImageHarness,
    topics: List[str],
    stable_seconds: float,
    timeout: float,
    max_gap_seconds: float,
    emit: StatusLogger,
    monitor: Optional[ImageSaver] = None,
    recovery_peer_wait_seconds: Optional[float] = None,
    recovery_startup_grace_seconds: Optional[float] = None,
    on_resubscribe: Optional[Callable[[Sequence[str], float], None]] = None,
    recovery_attempted_topics: Optional[Sequence[str]] = None,
) -> tuple[bool, List[Dict[str, Any]], str]:
    owns_monitor = monitor is None
    if monitor is None:
        monitor = StableImageMonitor(harness, topics, emit)
        snapshot = monitor.snapshot
    else:
        if set(monitor.state) != set(topics):
            raise ValueError("persistent image monitor topics do not match stability topics")
        drain_inactive_subscriptions(harness, len(topics))
        monitor.begin_stability_window()
        snapshot = monitor.stream_snapshot
    started_at = time.monotonic()
    deadline = started_at + timeout
    attempted_recovery_topics = set(recovery_attempted_topics or [])
    streams_detected_logged = False
    try:
        while time.monotonic() < deadline:
            session.assert_running()
            harness.spin_once(0.1)
            if not owns_monitor:
                elapsed = time.monotonic() - started_at
                effective_startup_grace = (
                    min(float(recovery_startup_grace_seconds), timeout / 2.0)
                    if recovery_startup_grace_seconds is not None
                    else None
                )
                recovery_topics = monitor.image_recovery_candidates(
                    elapsed=elapsed,
                    peer_wait_seconds=recovery_peer_wait_seconds,
                    startup_grace_seconds=effective_startup_grace,
                    excluded_topics=attempted_recovery_topics,
                )
                if recovery_topics:
                    recreated = monitor.recreate_image_subscriptions(recovery_topics)
                    attempted_recovery_topics.update(recreated)
                    if recreated and on_resubscribe is not None:
                        on_resubscribe(recreated, elapsed)
            if not streams_detected_logged and monitor.all_streams_detected():
                emit(f"streams detected, checking {stable_seconds:.1f}s stability")
                streams_detected_logged = True
            if monitor.stable_for(stable_seconds, max_gap_seconds):
                return True, snapshot(), "image streams are stable"
        return False, snapshot(), f"image streams were not stable within {timeout:.1f}s"
    finally:
        if owns_monitor:
            monitor.close()
        else:
            monitor.end_capture()


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


STREAM_DIRECTORY_NAMES = {
    "color": "color",
    "depth": "depth",
    "ir": "ir",
    "left_ir": "ir_left",
    "right_ir": "ir_right",
    "left_color": "color_left",
    "right_color": "color_right",
}
IMAGE_TRANSPORT_DIRECTORY_SUFFIXES = {
    "compressed": "compressed",
    "compresseddepth": "compressed_depth",
}
IMAGE_FILE_PATTERN = re.compile(r"^image_(\d+)\.(?:png|jpg)$", re.IGNORECASE)


def sanitize_path_part(value: str) -> str:
    text = value.strip().strip("/")
    if not text:
        return "unknown"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)


def _image_topic_parts(topic: str) -> tuple[str, str]:
    parts = [part for part in topic.strip().split("/") if part]
    transport_suffix = ""
    if parts:
        transport_suffix = IMAGE_TRANSPORT_DIRECTORY_SUFFIXES.get(
            parts[-1].lower(), ""
        )
        if transport_suffix:
            parts = parts[:-1]
    for index in range(len(parts) - 1, -1, -1):
        image_part = parts[index]
        if not image_part.startswith("image") or index == 0:
            continue
        stream_part = parts[index - 1]
        camera_name = parts[index - 2] if index > 1 else "unknown_camera"
        stream_name = STREAM_DIRECTORY_NAMES.get(
            stream_part, sanitize_path_part(stream_part)
        )
        image_variant = image_part[len("image") :].strip("_")
        if image_variant and image_variant != "raw":
            stream_name += "_" + sanitize_path_part(image_variant)
        if transport_suffix:
            stream_name += "_" + transport_suffix
        return camera_name, stream_name
    for index, part in enumerate(parts):
        if part in STREAM_DIRECTORY_NAMES:
            camera_name = parts[index - 1] if index > 0 else "unknown_camera"
            return camera_name, STREAM_DIRECTORY_NAMES[part]
    return (parts[0] if parts else "unknown_camera", parts[-1] if parts else "image")


class ImagePathSequence:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self._next_indices: Dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def next_path(self, topic: str, suffix: str) -> Path:
        with self._lock:
            camera_name, stream_name = _image_topic_parts(topic)
            key = (camera_name, stream_name)
            stream_dir = ensure_dir(self.output_root / camera_name / stream_name)
            next_index = self._next_indices.get(key)
            if next_index is None:
                indices = [
                    int(match.group(1))
                    for path in stream_dir.iterdir()
                    if path.is_file() and (match := IMAGE_FILE_PATTERN.match(path.name))
                ]
                next_index = max(indices, default=0) + 1
            target = stream_dir / f"image_{next_index:04d}{suffix}"
            while target.exists():
                next_index += 1
                target = stream_dir / f"image_{next_index:04d}{suffix}"
            self._next_indices[key] = next_index + 1
            return target


def colorize_depth_image(image: Any, cv2: Any) -> Any:
    import numpy as np

    values = np.asarray(image)
    valid = np.isfinite(values) & (values > 0)
    normalized = np.zeros(values.shape, dtype=np.uint8)
    if np.any(valid):
        low, high = np.percentile(values[valid], [1, 99])
        if high > low:
            normalized[valid] = np.clip(
                (values[valid].astype(np.float64) - low) / (high - low) * 255.0,
                0,
                255,
            ).astype(np.uint8)
        else:
            normalized[valid] = 255
    rendered = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
    rendered[~valid] = 0
    return rendered


def is_depth_image_topic(topic: str) -> bool:
    return any(
        part == "depth" or part.startswith("depth_")
        for part in topic.strip("/").lower().split("/")
    )


class ImageSaver:
    def __init__(
        self,
        harness: RosImageHarness,
        topics: List[str],
        output_root: Path,
        count: int,
        skip_frames: int = 0,
        path_sequence: Optional[ImagePathSequence] = None,
        active: bool = True,
    ) -> None:
        self.harness = harness
        self.count = count
        self.skip_frames = skip_frames
        self.paths = path_sequence or ImagePathSequence(output_root)
        self.topic_kinds = {
            topic: harness.resolve_image_topic_kind(topic) for topic in topics
        }
        self.state: Dict[str, Dict[str, Any]] = self._new_state()
        self.subscriptions = []
        self.image_subscriptions: Dict[str, Any] = {}
        self.resubscribed_image_topics: List[str] = []
        self._bridge = None
        self._cv2 = None
        self._state_lock = threading.Lock()
        self._state_changed = threading.Condition(self._state_lock)
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-writer")
        self._frozen = False
        self._active = bool(active)
        for topic in topics:
            subscription = self._create_image_subscription(topic)
            self.image_subscriptions[topic] = subscription
            self.subscriptions.append(subscription)

    def _new_state(self) -> Dict[str, Dict[str, Any]]:
        return {
            topic: {
                "topic_kind": topic_kind,
                "files": [],
                "error": "",
                "pending_saves": 0,
                "selected_count": 0,
                "received_count": 0,
                "skipped_count": 0,
                "buffer": deque(maxlen=self.count),
                "errors": [],
                "message_count": 0,
                "first_message_at": None,
                "last_message_at": None,
                "width": 0,
                "height": 0,
                "data_size": 0,
            }
            for topic, topic_kind in self.topic_kinds.items()
        }

    def _create_image_subscription(self, topic: str) -> Any:
        return self.harness.create_image_subscription(
            topic,
            lambda message, topic_name=topic: self._on_message(topic_name, message),
            topic_kind=self.topic_kinds[topic],
        )

    def image_recovery_candidates(
        self,
        *,
        elapsed: float,
        peer_wait_seconds: Optional[float],
        startup_grace_seconds: Optional[float],
        excluded_topics: Sequence[str],
    ) -> List[str]:
        excluded = set(excluded_topics)
        now = time.monotonic()
        with self._state_lock:
            missing = [
                topic
                for topic, item in self.state.items()
                if item["first_message_at"] is None and topic not in excluded
            ]
            if (
                startup_grace_seconds is not None
                and elapsed >= max(float(startup_grace_seconds), 0.0)
            ):
                return missing
            if peer_wait_seconds is None:
                return []
            candidates: List[str] = []
            for topic in missing:
                camera_name, _stream_name = _image_topic_parts(topic)
                peer_first_messages = [
                    item["first_message_at"]
                    for peer_topic, item in self.state.items()
                    if peer_topic != topic
                    and _image_topic_parts(peer_topic)[0] == camera_name
                    and item["first_message_at"] is not None
                ]
                if peer_first_messages and now - min(peer_first_messages) >= max(
                    float(peer_wait_seconds), 0.0
                ):
                    candidates.append(topic)
            return candidates

    def recreate_image_subscriptions(self, topics: Sequence[str]) -> List[str]:
        recreated: List[str] = []
        for topic in topics:
            old_subscription = self.image_subscriptions.get(topic)
            if old_subscription is None:
                continue
            self.harness.destroy_subscription(old_subscription)
            if old_subscription in self.subscriptions:
                self.subscriptions.remove(old_subscription)
            new_subscription = self._create_image_subscription(topic)
            self.image_subscriptions[topic] = new_subscription
            self.subscriptions.append(new_subscription)
            recreated.append(topic)
        self.resubscribed_image_topics.extend(recreated)
        return recreated

    def begin_capture(self, skip_frames: Optional[int] = None) -> None:
        with self._state_changed:
            while any(item["pending_saves"] for item in self.state.values()):
                self._state_changed.wait()
            if skip_frames is not None:
                self.skip_frames = max(int(skip_frames), 0)
            self.state = self._new_state()
            self.resubscribed_image_topics = []
            self._frozen = False
            self._active = True

    def begin_stability_window(self) -> None:
        with self._state_lock:
            for item in self.state.values():
                item.update(
                    {
                        "message_count": 0,
                        "first_message_at": None,
                        "last_message_at": None,
                        "width": 0,
                        "height": 0,
                        "data_size": 0,
                    }
                )
            self._frozen = True
            self._active = True
            self.resubscribed_image_topics = []

    def end_capture(self) -> None:
        with self._state_lock:
            self._active = False

    def _write(self, topic: str, message: Any, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.state[topic]["topic_kind"] == "compressed":
            target.write_bytes(bytes(getattr(message, "data", b"") or b""))
            return
        if self._bridge is None:
            try:
                import cv2
                from cv_bridge import CvBridge
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "saving raw images requires cv_bridge and OpenCV; set "
                    f"--save-image-count 0 to disable saving: {exc}"
                ) from exc
            self._bridge = CvBridge()
            self._cv2 = cv2
        encoding = str(getattr(message, "encoding", "") or "")
        image = self._bridge.imgmsg_to_cv2(message, desired_encoding="passthrough")
        if is_depth_image_topic(topic) and encoding.lower() in {"16uc1", "mono16"}:
            image = colorize_depth_image(image, self._cv2)
        elif encoding.lower() == "rgb8":
            image = self._cv2.cvtColor(image, self._cv2.COLOR_RGB2BGR)
        elif encoding.lower() == "rgba8":
            image = self._cv2.cvtColor(image, self._cv2.COLOR_RGBA2BGRA)
        if not self._cv2.imwrite(
            str(target), image, [int(self._cv2.IMWRITE_PNG_COMPRESSION), 1]
        ):
            raise RuntimeError(f"failed to write PNG image: {target}")

    def _on_message(self, topic: str, message: Any) -> None:
        now = time.monotonic()
        with self._state_lock:
            if not self._active:
                return
            item = self.state[topic]
            item["message_count"] += 1
            item["last_message_at"] = now
            item["width"] = int(getattr(message, "width", 0) or 0)
            item["height"] = int(getattr(message, "height", 0) or 0)
            item["data_size"] = len(getattr(message, "data", b"") or b"")
            has_valid_payload = (
                item["data_size"] > 0
                if item["topic_kind"] == "compressed"
                else item["width"] > 0 and item["height"] > 0
            )
            if item["first_message_at"] is None and has_valid_payload:
                item["first_message_at"] = now
            item["received_count"] += 1
            if item["skipped_count"] < self.skip_frames:
                item["skipped_count"] += 1
                return
            if (
                self._frozen
                or item["error"]
                or len(item["buffer"]) >= self.count
            ):
                return
            item["buffer"].append(message)

    def all_streams_detected(self) -> bool:
        with self._state_lock:
            return all(
                item["first_message_at"] is not None for item in self.state.values()
            )

    def stable_for(self, stable_seconds: float, max_gap_seconds: float) -> bool:
        now = time.monotonic()
        with self._state_lock:
            for item in self.state.values():
                first = item["first_message_at"]
                last = item["last_message_at"]
                if first is None or last is None:
                    return False
                if now - last > max_gap_seconds:
                    item["first_message_at"] = None
                    return False
                if now - first < stable_seconds:
                    return False
            return True

    def stream_snapshot(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._state_lock:
            return [
                {
                    "name": topic,
                    "topic_kind": item["topic_kind"],
                    "message_count": item["message_count"],
                    "width": item["width"],
                    "height": item["height"],
                    "data_size": item["data_size"],
                    "seconds_since_last_message": (
                        now - item["last_message_at"]
                        if item["last_message_at"] is not None
                        else None
                    ),
                    "stable_seconds": (
                        now - item["first_message_at"]
                        if item["first_message_at"] is not None
                        else 0.0
                    ),
                }
                for topic, item in self.state.items()
            ]

    def buffer_ready(self) -> bool:
        with self._state_lock:
            return all(
                len(item["buffer"]) >= self.count
                for item in self.state.values()
            )

    def submit_first_frames(self) -> None:
        with self._state_lock:
            if self._frozen:
                return
            if not all(
                len(item["buffer"]) >= self.count
                for item in self.state.values()
            ):
                return
            self._frozen = True
            for topic, item in self.state.items():
                messages = list(item["buffer"])
                item["buffer"].clear()
                item["selected_count"] = len(messages)
                item["pending_saves"] = len(messages)
                for message in messages:
                    try:
                        self._writer.submit(self._save_message, topic, message)
                    except Exception as exc:  # noqa: BLE001
                        error = str(exc)
                        if not item["error"]:
                            item["error"] = error
                        item["errors"].append(error)
                        item["pending_saves"] -= 1
            self._state_changed.notify_all()

    def _save_message(self, topic: str, message: Any) -> None:
        try:
            suffix = (
                ".jpg"
                if self.state[topic]["topic_kind"] == "compressed"
                else ".png"
            )
            target = self.paths.next_path(topic, suffix)
            self._write(topic, message, target)
            with self._state_lock:
                self.state[topic]["files"].append(str(target))
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                error = str(exc)
                if not self.state[topic]["error"]:
                    self.state[topic]["error"] = error
                self.state[topic]["errors"].append(error)
        finally:
            with self._state_changed:
                self.state[topic]["pending_saves"] -= 1
                self._state_changed.notify_all()

    def wait_for_pending_save(self, timeout: float) -> None:
        with self._state_changed:
            if any(item["pending_saves"] for item in self.state.values()):
                self._state_changed.wait(timeout=max(float(timeout), 0.0))

    def first_error(self) -> str:
        with self._state_lock:
            for topic, item in self.state.items():
                if item["error"]:
                    return f"{topic}: {item['error']}"
        return ""

    def saving_finished(self) -> bool:
        with self._state_lock:
            return self._frozen and not any(
                item["pending_saves"] for item in self.state.values()
            )

    def complete(self) -> bool:
        with self._state_lock:
            return all(
                len(item["files"]) >= self.count for item in self.state.values()
            )

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._state_lock:
            return [
                {
                    "topic": topic,
                    "topic_kind": item["topic_kind"],
                    "files": list(item["files"]),
                    "saved_count": len(item["files"]),
                    "expected_count": self.count,
                    "received_count": item["received_count"],
                    "skipped_count": item["skipped_count"],
                    "selected_count": item["selected_count"],
                    "buffered_count": len(item["buffer"]),
                    "error": item["error"],
                    "errors": list(item["errors"]),
                }
                for topic, item in self.state.items()
            ]

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []
        self.image_subscriptions = {}
        self._writer.shutdown(wait=True, cancel_futures=True)


def save_images(
    *,
    session: LaunchSession,
    harness: RosImageHarness,
    topics: List[str],
    output_root: Path,
    count: int,
    skip_frames: int = 0,
    timeout: float,
    path_sequence: Optional[ImagePathSequence] = None,
    saver: Optional[ImageSaver] = None,
    recovery_peer_wait_seconds: Optional[float] = None,
    recovery_startup_grace_seconds: Optional[float] = None,
    on_resubscribe: Optional[Callable[[Sequence[str], float], None]] = None,
) -> tuple[bool, List[Dict[str, Any]], str]:
    if count <= 0:
        return True, [], "image saving disabled"
    owns_saver = saver is None
    if saver is None:
        saver = ImageSaver(
            harness,
            topics,
            output_root,
            count,
            skip_frames=skip_frames,
            path_sequence=path_sequence,
            active=False,
        )
    if set(saver.state) != set(topics) or saver.count != count:
        raise ValueError("persistent image saver configuration does not match capture")
    if not owns_saver:
        drain_inactive_subscriptions(harness, len(topics))
    saver.begin_capture(skip_frames=skip_frames)
    started_at = time.monotonic()
    deadline = started_at + timeout
    recovery_attempted_topics: set[str] = set()
    try:
        while time.monotonic() < deadline and not saver.buffer_ready():
            session.assert_running()
            harness.spin_once(0.1)
            elapsed = time.monotonic() - started_at
            effective_startup_grace = (
                min(float(recovery_startup_grace_seconds), timeout / 2.0)
                if recovery_startup_grace_seconds is not None
                else None
            )
            recovery_topics = saver.image_recovery_candidates(
                elapsed=elapsed,
                peer_wait_seconds=recovery_peer_wait_seconds,
                startup_grace_seconds=effective_startup_grace,
                excluded_topics=recovery_attempted_topics,
            )
            if recovery_topics:
                recreated = saver.recreate_image_subscriptions(recovery_topics)
                recovery_attempted_topics.update(recreated)
                if recreated and on_resubscribe is not None:
                    on_resubscribe(recreated, elapsed)
        if not saver.buffer_ready():
            return (
                False,
                saver.snapshot(),
                f"did not receive {count} image(s) per topic after skipping "
                f"{skip_frames} frame(s) within {timeout:.1f}s",
            )
        saver.submit_first_frames()
        while not saver.saving_finished():
            session.assert_running()
            harness.spin_once(0.1)
            saver.wait_for_pending_save(0.01)
        error = saver.first_error()
        if error:
            return False, saver.snapshot(), error
        snapshot = saver.snapshot()
        return True, snapshot, f"saved {sum(len(row['files']) for row in snapshot)} image file(s)"
    finally:
        saver.end_capture()
        if owns_saver:
            saver.close()


def expand_camera_topic(topic: str, camera_name: str) -> str:
    return topic.replace("{camera}", camera_name).replace("${camera}", camera_name)


def stream_failure_details(
    snapshot: Sequence[Dict[str, Any]],
    *,
    stable_seconds: float,
    max_gap_seconds: float,
) -> List[Dict[str, str]]:
    details: List[Dict[str, str]] = []
    for row in snapshot:
        topic = str(row.get("topic") or row.get("name") or "")
        count = int(row.get("message_count", 0) or 0)
        age = row.get("seconds_since_last_message")
        stable_for = float(row.get("stable_seconds", 0.0) or 0.0)
        reason = ""
        if count == 0:
            reason = "no frames received"
        elif age is not None and float(age) > max_gap_seconds:
            reason = (
                f"stream stalled; last frame was {float(age):.1f}s ago "
                f"(maximum {max_gap_seconds:.1f}s)"
            )
        elif stable_for < stable_seconds:
            reason = f"stable for only {stable_for:.1f}s; required {stable_seconds:.1f}s"
        if reason:
            details.append(
                {"camera": _image_topic_parts(topic)[0], "topic": topic, "reason": reason}
            )
    return details


def sensor_failure_details(snapshot: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    details: List[Dict[str, str]] = []
    for row in snapshot:
        expected = max(int(row.get("expected_count", 0) or 0), 1)
        kind = str(row.get("kind", "sensor"))
        completed = (
            int(row.get("valid_message_count", 0) or 0)
            if kind == "point_cloud"
            else int(row.get("completed_windows", 0) or 0)
        )
        error = str(row.get("error", "") or "")
        if error or completed < expected:
            details.append(
                {
                    "camera": str(row.get("camera", "") or "unknown"),
                    "topic": str(row.get("topic") or row.get("name") or ""),
                    "reason": error or f"received {completed}/{expected} required {kind} sample(s)",
                }
            )
    return details


def emit_failure_details(emit: StatusLogger, details: Sequence[Dict[str, str]]) -> None:
    for detail in details:
        emit(
            "[FAIL] "
            f"camera={detail.get('camera', 'unknown')} "
            f"topic={detail.get('topic', '')}: {detail.get('reason', 'check failed')}"
        )


def stop_launch_attempt(session, attempt, result, emit) -> bool:
    """Stop launch and record shutdown timing without clearing earlier failures."""
    started = time.monotonic()
    try:
        session.stop()
    finally:
        elapsed = time.monotonic() - started
        # A cleanup retry must not replace the original shutdown measurement.
        if attempt is not None and "stop_seconds" not in attempt:
            attempt["stop_seconds"] = elapsed
            attempt["ended_at"] = datetime.now().isoformat(timespec="seconds")
            status = "failed" if elapsed > 10.0 else "warning" if elapsed > 5.0 else "passed"
            attempt["stop_status"] = status
            message = f"launch stop took {elapsed:.3f}s ({status}; warning >5s, failure >10s)"
            emit(
                f"[LAUNCH_STOP][{status.upper()}] attempt {attempt['attempt']}: {message}",
                event="warning" if status == "warning" else "log",
                attempt=attempt["attempt"],
            )
            if status == "failed":
                attempt["status"] = "failed"
                attempt["message"] = "; ".join(filter(None, [attempt.get("message"), message]))
                attempt.setdefault("failure_details", []).append(
                    {"camera": "launch", "topic": "launch shutdown", "reason": message}
                )
                result["status"] = "failed"
                result.setdefault("errors", []).append(message)
            elif status == "warning":
                warning = {"attempt": attempt["attempt"], "message": message}
                attempt.setdefault("warnings", []).append(warning)
                result.setdefault("warnings", []).append(warning)
    return attempt is None or attempt.get("stop_status") != "failed"


def build_summary(result: Dict[str, Any]) -> str:
    command = result.get("command", [])
    command_text = " ".join(shlex.quote(str(item)) for item in command) if command else ""
    topics = result.get("image_topics", [])
    failed_attempts = [
        attempt
        for attempt in result.get("attempts", [])
        if attempt.get("status") == "failed"
    ]
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
        f"- Failed restarts: {len(failed_attempts)}",
        f"- Launch attempts: {result.get('launch_attempts', 0)}",
        f"- Visual artifacts per topic per restart: {result.get('save_image_count', 0)}",
        f"- Messages skipped per artifact topic per restart: {result.get('skip_image_frames', 0)}",
        f"- Elapsed seconds: {float(result.get('elapsed_seconds', 0.0) or 0.0):.1f}",
        "",
        "## Failed Attempts",
        "",
    ]
    if failed_attempts:
        lines.extend(
            [
                "| Attempt | Started At | Reason | Launch Log |",
                "| ---: | --- | --- | --- |",
            ]
        )
        for attempt in failed_attempts:
            attempt_number = attempt.get("attempt", "")
            launch_log = str(attempt.get("launch_log", "") or "")
            if launch_log and str(attempt_number).isdigit():
                launch_log = (
                    f"logs/test_{int(attempt_number):04d}/{Path(launch_log).name}"
                )
            cells = [
                attempt_number,
                attempt.get("started_at", ""),
                attempt.get("message", ""),
                launch_log,
            ]
            escaped_cells = [
                str(cell).replace("\n", "<br>").replace("|", "\\|")
                for cell in cells
            ]
            lines.append("| " + " | ".join(escaped_cells) + " |")
    else:
        lines.append("- None")
    failure_details = [
        detail
        for attempt in failed_attempts
        for detail in attempt.get("failure_details", [])
        if isinstance(detail, dict)
    ]
    if failure_details:
        lines.extend(["", "## Failure Details", ""])
        for detail in failure_details:
            lines.append(
                f"- camera `{detail.get('camera', 'unknown')}`, "
                f"topic `{detail.get('topic', '')}`: {detail.get('reason', '')}"
            )
    warnings = [
        warning
        for warning in result.get("warnings", [])
        if isinstance(warning, dict)
    ]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in warnings:
            topic_text = ", ".join(
                f"`{topic}`" for topic in warning.get("topics", [])
            )
            topic_suffix = f", topics {topic_text}" if topic_text else ""
            lines.append(
                f"- attempt `{warning.get('attempt', '')}`"
                f"{topic_suffix}: {warning.get('message', '')}"
            )
    lines.extend(
        [
            "",
            "## Monitored Streams",
            "",
        ]
    )
    if topics:
        lines.extend(f"- {topic}" for topic in topics)
    else:
        lines.append("- None")
    lines.extend(["", "## Point Cloud Streams", ""])
    lines.extend(f"- {topic}" for topic in result.get("point_cloud_topics", []))
    if not result.get("point_cloud_topics"):
        lines.append("- None")
    lines.extend(["", "## IMU Streams", ""])
    lines.extend(f"- {topic}" for topic in result.get("imu_topics", []))
    if not result.get("imu_topics"):
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

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_restart_stream") + f"_v{TOOL_VERSION}"
    run_started_at = iso_now()
    default_results_dir = Path(__file__).resolve().parent / "results" / run_id
    results_dir = ensure_dir(Path(args.results_dir).resolve() if args.results_dir else default_results_dir)
    image_paths = ImagePathSequence(results_dir / "images")
    sensor_paths = SensorArtifactPathSequence(results_dir / "images")
    install_terminal_log(results_dir / "terminal.log")
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

    duration_text = str(args.duration or "").strip()
    run_count = args.run_count
    if not duration_text and run_count is None:
        raise ValueError("at least one of --duration or --run-count is required")
    duration_seconds = parse_duration(duration_text, 0.0) if duration_text else None
    stable_seconds = parse_duration(args.stable_seconds, 5.0)
    stream_timeout = parse_duration(args.stream_timeout, 20.0)
    topic_discovery_timeout = parse_duration(args.topic_discovery_timeout, 15.0)
    max_gap_seconds = parse_duration(args.max_gap_seconds, 1.5)
    save_image_count = int(args.save_image_count)
    if save_image_count < 0:
        raise ValueError("--save-image-count must be >= 0")
    skip_image_frames = int(args.skip_image_frames)
    if skip_image_frames < 0:
        raise ValueError("--skip-image-frames must be >= 0")
    save_image_timeout = parse_duration(args.save_image_timeout, 30.0)
    restart_delay = float(args.restart_delay)
    if run_count is not None and run_count <= 0:
        raise ValueError("--run-count must be > 0")
    deadline = (
        time.monotonic() + duration_seconds
        if duration_seconds is not None
        else None
    )

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
        "point_cloud_topics": [],
        "imu_topics": [],
        "save_image_count": save_image_count,
        "skip_image_frames": skip_image_frames,
        "duration_seconds": duration_seconds,
        "run_count": run_count,
        "continue_on_failure": args.continue_on_failure,
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
    if save_image_count > 0:
        emit(
            f"save {save_image_count} image(s) per topic after skipping the first "
            f"{skip_image_frames} message(s) per artifact topic"
        )
    emit(
        f"planned duration: {duration_seconds:.1f}s"
        if duration_seconds is not None
        else "planned duration: unlimited"
    )
    if auto_discover_topics:
        emit("monitor topics: auto discover on first launch")
    else:
        emit(
            f"monitor topics: {', '.join(explicit_topics)} "
            f"(stable {stable_seconds:.1f}s, timeout {stream_timeout:.1f}s per launch)"
        )

    attempt_index = 0
    monitored_topics = list(explicit_topics)
    sensor_baseline: Optional[tuple[List[str], List[str], Dict[str, str]]] = None
    image_monitor: Optional[ImageSaver] = None
    sensor_monitor: Optional[SensorCaptureMonitor] = None
    test_start_monotonic = time.monotonic()
    active_session: Optional[LaunchSession] = None
    current_attempt: Optional[Dict[str, Any]] = None
    keep_launch_running = False
    try:
        with RosImageHarness(
            args.ros_version, "launch_restart_stream_check", args.queue_size
        ) as harness, ExitStack() as resources:
            while deadline is None or time.monotonic() < deadline:
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
                    "images": [],
                    "sensors": [],
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
                if image_monitor is None:
                    image_monitor = ImageSaver(
                        harness,
                        monitored_topics,
                        results_dir / "images",
                        save_image_count,
                        skip_frames=skip_image_frames,
                        path_sequence=image_paths,
                        active=False,
                    )
                    resources.callback(image_monitor.close)
                    emit("persistent image subscriptions are ready")
                resubscribed_image_topics: List[str] = []

                def log_image_resubscribe(
                    topics: Sequence[str], elapsed: float
                ) -> None:
                    resubscribed_image_topics.extend(topics)
                    emit(
                        f"[IMAGE][WARN] attempt {attempt_index}: no messages from "
                        f"{', '.join(topics)} after {elapsed:.1f}s; recreating subscription(s)"
                    )

                image_ok, image_snapshot, image_message = save_images(
                    session=session,
                    harness=harness,
                    topics=monitored_topics,
                    output_root=results_dir / "images",
                    count=save_image_count,
                    skip_frames=skip_image_frames,
                    timeout=save_image_timeout,
                    path_sequence=image_paths,
                    saver=image_monitor,
                    recovery_peer_wait_seconds=IMAGE_RESUBSCRIBE_AFTER_PEER_SECONDS,
                    recovery_startup_grace_seconds=(
                        IMAGE_RESUBSCRIBE_STARTUP_GRACE_SECONDS
                    ),
                    on_resubscribe=log_image_resubscribe,
                )
                attempt["images"] = image_snapshot
                if not image_ok:
                    warning = {
                        "attempt": attempt_index,
                        "action": "save-images",
                        "message": image_message,
                    }
                    attempt["image_save_status"] = "failed"
                    attempt["image_save_message"] = image_message
                    attempt.setdefault("warnings", []).append(warning)
                    result.setdefault("warnings", []).append(warning)
                    emit(
                        f"[IMAGE][WARN] attempt {attempt_index}: {image_message}; "
                        "continuing stress test"
                    )
                else:
                    attempt["image_save_status"] = "passed"
                    attempt["image_save_message"] = image_message

                ok, snapshot, message = wait_for_stable_streams(
                    session=session,
                    harness=harness,
                    topics=monitored_topics,
                    stable_seconds=stable_seconds,
                    timeout=stream_timeout,
                    max_gap_seconds=max_gap_seconds,
                    emit=emit,
                    monitor=image_monitor,
                    recovery_peer_wait_seconds=IMAGE_RESUBSCRIBE_AFTER_PEER_SECONDS,
                    recovery_startup_grace_seconds=(
                        IMAGE_RESUBSCRIBE_STARTUP_GRACE_SECONDS
                    ),
                    on_resubscribe=log_image_resubscribe,
                    recovery_attempted_topics=resubscribed_image_topics,
                )
                attempt["topics"] = snapshot
                attempt["message"] = message
                if resubscribed_image_topics:
                    recovered_topics = sorted(set(resubscribed_image_topics))
                    outcome = "recovered" if ok else "failed"
                    warning = {
                        "attempt": attempt_index,
                        "action": "recreate-image-subscription",
                        "topics": recovered_topics,
                        "outcome": outcome,
                        "message": (
                            "image streams recovered after subscription recreation; "
                            "attempt accepted"
                            if ok
                            else "image streams did not recover after subscription recreation"
                        ),
                    }
                    attempt.setdefault("warnings", []).append(warning)
                    result.setdefault("warnings", []).append(warning)
                    emit(
                        f"[IMAGE][WARN] attempt {attempt_index}: {outcome} after "
                        f"subscription recreation: {', '.join(recovered_topics)}"
                        + ("; accepting attempt" if ok else "")
                    )
                attempt["stable_seconds"] = min(
                    (item.get("stable_seconds", 0.0) or 0.0 for item in snapshot),
                    default=0.0,
                )
                if not ok:
                    failure_details = stream_failure_details(
                        snapshot,
                        stable_seconds=stable_seconds,
                        max_gap_seconds=max_gap_seconds,
                    )
                    attempt["failure_details"] = failure_details
                    emit_failure_details(emit, failure_details)
                    attempt["status"] = "failed"
                    result["status"] = "failed"
                    result.setdefault("errors", []).append(
                        f"attempt {attempt_index}: {message}"
                    )
                    attempt["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    emit_failed_attempt(
                        emit,
                        f"attempt {attempt_index}: failed"
                        + (
                            "; stopping launch and continuing with the next cycle"
                            if args.continue_on_failure
                            else ""
                        ),
                        attempt_index=attempt_index,
                        run_count=run_count,
                    )
                    if args.continue_on_failure:
                        stop_launch_attempt(session, attempt, result, emit)
                        active_session = None
                        current_attempt = None
                        if deadline is None:
                            time.sleep(restart_delay)
                        else:
                            time.sleep(
                                min(
                                    restart_delay,
                                    max(deadline - time.monotonic(), 0.0),
                                )
                            )
                        continue
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

                if sensor_baseline is None:
                    camera_names = sorted(
                        {_image_topic_parts(topic)[0] for topic in monitored_topics}
                    ) or [template_camera_name]
                    configured_point_cloud_topics = expand_topic_templates(
                        args.point_cloud_topic, camera_names
                    )
                    configured_imu_topics = expand_topic_templates(
                        args.imu_topic, camera_names
                    )
                    sensor_baseline = discover_sensor_topics(
                        harness=harness,
                        camera_names=camera_names,
                        point_cloud_topics=configured_point_cloud_topics,
                        imu_topics=configured_imu_topics,
                        timeout=topic_discovery_timeout,
                        ensure_running=session.assert_running,
                    )
                    result["point_cloud_topics"] = sensor_baseline[0]
                    result["imu_topics"] = sensor_baseline[1]
                    emit(
                        f"sensor baseline: {len(sensor_baseline[0])} point cloud, "
                        f"{len(sensor_baseline[1])} IMU topic(s)"
                    )
                point_cloud_topics, imu_topics, sensor_topic_cameras = sensor_baseline
                if sensor_monitor is None and (point_cloud_topics or imu_topics):
                    sensor_monitor = SensorCaptureMonitor(
                        harness=harness,
                        point_cloud_topics=point_cloud_topics,
                        imu_topics=imu_topics,
                        topic_cameras=sensor_topic_cameras,
                        output_root=results_dir / "images",
                        save_count=save_image_count,
                        skip_frames=skip_image_frames,
                        active=False,
                        path_sequence=sensor_paths,
                    )
                    resources.callback(sensor_monitor.close)
                    emit("persistent point cloud/IMU subscriptions are ready")
                sensor_timeout = max(
                    save_image_timeout,
                    2.0 * max(save_image_count, 1) + 5.0,
                )
                resubscribed_point_cloud_topics: List[str] = []

                def log_point_cloud_resubscribe(
                    topics: Sequence[str], elapsed: float
                ) -> None:
                    resubscribed_point_cloud_topics.extend(topics)
                    emit(
                        f"[POINT_CLOUD][WARN] attempt {attempt_index}: no messages from "
                        f"{', '.join(topics)} after {elapsed:.1f}s; recreating subscription(s)"
                    )

                sensor_ok, sensor_snapshot, sensor_message = capture_sensor_artifacts(
                    harness=harness,
                    point_cloud_topics=point_cloud_topics,
                    imu_topics=imu_topics,
                    topic_cameras=sensor_topic_cameras,
                    output_root=results_dir / "images",
                    save_count=save_image_count,
                    timeout=sensor_timeout,
                    skip_frames=skip_image_frames,
                    path_sequence=sensor_paths,
                    ensure_running=session.assert_running,
                    monitor=sensor_monitor,
                    resubscribe_after_seconds=POINT_CLOUD_RESUBSCRIBE_AFTER_SECONDS,
                    on_resubscribe=log_point_cloud_resubscribe,
                )
                attempt["sensors"] = sensor_snapshot
                if sensor_ok and resubscribed_point_cloud_topics:
                    recovered_topics = sorted(set(resubscribed_point_cloud_topics))
                    warning = {
                        "attempt": attempt_index,
                        "action": "recreate-point-cloud-subscription",
                        "topics": recovered_topics,
                        "message": (
                            "point cloud recovered after subscription recreation; "
                            "attempt accepted"
                        ),
                    }
                    attempt.setdefault("warnings", []).append(warning)
                    result.setdefault("warnings", []).append(warning)
                    emit(
                        f"[POINT_CLOUD][WARN] attempt {attempt_index}: recovered after "
                        f"subscription recreation: {', '.join(recovered_topics)}; "
                        "accepting attempt"
                    )
                if not sensor_ok:
                    failure_details = sensor_failure_details(sensor_snapshot)
                    attempt["failure_details"] = failure_details
                    emit_failure_details(emit, failure_details)
                    attempt["status"] = "failed"
                    attempt["message"] = sensor_message
                    attempt["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    result["status"] = "failed"
                    result.setdefault("errors", []).append(sensor_message)
                    emit_failed_attempt(
                        emit,
                        f"attempt {attempt_index}: {sensor_message}"
                        + (
                            "; continuing with the next cycle"
                            if args.continue_on_failure
                            else ""
                        ),
                        attempt_index=attempt_index,
                        run_count=run_count,
                    )
                    if args.continue_on_failure:
                        stop_launch_attempt(session, attempt, result, emit)
                        active_session = None
                        current_attempt = None
                        continue
                    raise RuntimeError(sensor_message)
                attempt["message"] = (
                    f"{message}; {image_message}; {sensor_message}"
                )

                emit(f"attempt {attempt_index}: stream checks passed, stopping launch")
                stop_ok = stop_launch_attempt(session, attempt, result, emit)
                active_session = None
                current_attempt = None
                if stop_ok:
                    attempt["status"] = attempt["stop_status"]
                    result["successful_restarts"] += 1
                    emit(
                        f"attempt {attempt_index}: "
                        + ("warning" if attempt.get("stop_status") == "warning" else "passed"),
                        event="progress",
                        current=attempt_index,
                        total=run_count,
                        phase="completed-cycle",
                    )
                else:
                    emit_failed_attempt(
                        emit,
                        f"attempt {attempt_index}: {attempt['message']}",
                        attempt_index=attempt_index,
                        run_count=run_count,
                    )
                    if not args.continue_on_failure:
                        break

                if deadline is not None and time.monotonic() >= deadline:
                    break
                if deadline is None:
                    time.sleep(restart_delay)
                else:
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
                current_attempt.setdefault(
                    "failure_details",
                    [
                        {
                            "camera": template_camera_name or "unknown",
                            "topic": "launch/topic discovery",
                            "reason": str(exc),
                        }
                    ],
                )
                emit_failure_details(emit, current_attempt["failure_details"])
            emit(f"test failed: {exc}")
    finally:
        if active_session is not None and not keep_launch_running:
            emit("stop launch")
            stop_launch_attempt(active_session, current_attempt, result, emit)
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


def parse_args(argv=None):
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
        "--point-cloud-topic",
        action="append",
        default=[],
        help=(
            "PointCloud2 topic template to require after every restart; can repeat and "
            "supports {camera}. When omitted, topics are discovered on the first launch."
        ),
    )
    parser.add_argument(
        "--imu-topic",
        action="append",
        default=[],
        help=(
            "Imu topic template to require after every restart; can repeat and supports "
            "{camera}. When omitted, topics are discovered on the first launch."
        ),
    )
    parser.add_argument(
        "--topic-discovery-timeout",
        default="15",
        help="Max wait time for auto image topic discovery during the first launch attempt",
    )
    parser.add_argument("--duration", default="", help="Optional total test duration; supports seconds, 15m, 2h")
    parser.add_argument(
        "--run-count",
        type=int,
        default=None,
        help="Maximum completed restart cycles; duration still applies when both are set",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue with the next restart cycle after a failed cycle (default: stop)",
    )
    parser.add_argument("--stable-seconds", default="5", help="Required continuous stable image duration per launch")
    parser.add_argument("--stream-timeout", default="20", help="Max wait time for stable stream per launch")
    parser.add_argument("--max-gap-seconds", default="1.5", help="Max allowed receive gap between images")
    parser.add_argument("--restart-delay", default="2", help="Delay seconds between stop and next start")
    parser.add_argument(
        "--save-image-count",
        type=int,
        default=1,
        help=(
            "Artifacts per topic after every restart (image/IMU PNG, point cloud "
            "PLY); 0 keeps validation but disables saving"
        ),
    )
    parser.add_argument(
        "--skip-image-frames",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Skip the first N messages per image, point cloud, and IMU topic "
            "before capture (default: 0)"
        ),
    )
    parser.add_argument(
        "--save-image-timeout",
        default="30",
        help="Max wait time for image and sensor artifact capture",
    )
    parser.add_argument("--queue-size", type=int, default=10)
    parser.add_argument("--results-dir", default="")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(TOOL_VERSION),
    )
    args = parser.parse_args(argv)
    if not str(args.duration or "").strip() and args.run_count is None:
        parser.error("at least one of --duration or --run-count is required")
    return args


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
