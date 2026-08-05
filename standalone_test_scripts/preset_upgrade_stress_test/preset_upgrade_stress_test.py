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
from dataclasses import asdict, dataclass
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

ENV_READY_VAR = "PRESET_UPGRADE_STRESS_TEST_ENV_READY"
INTERRUPTED = False
SCRIPT_DIR = Path(__file__).resolve().parent
TOOL_VERSION = "1.3"
TEST_ID = "preset_upgrade_stress_test"
DEFAULT_STRESS_LAUNCH_ARGS = {
    "enable_heartbeat": "true",
    "enable_firmware_log": "true",
}
DEFAULT_PRESET_A_PATH = SCRIPT_DIR / "config" / "g336x_K_High_Confidence_0.0.2.bin"
DEFAULT_PRESET_B_PATH = SCRIPT_DIR / "config" / "g336x_K_High_Accuracy_0.0.2.bin"
DEFAULT_LAUNCH = {
    "1": "gemini_330_series.launch",
    "2": "gemini_330_series.launch.py",
}


@dataclass
class PresetSpec:
    key: str
    path: Path
    name: str


@dataclass
class CameraSpec:
    name: str
    usb_port: str = ""
    serial_number: str = ""
    device_ip: str = ""
    device_port: str = ""
    config_file_path: str = ""


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handle_sigint(signum, frame) -> None:
    del signum, frame
    global INTERRUPTED
    INTERRUPTED = True


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def parse_camera_spec(raw: str) -> CameraSpec:
    return CameraSpec(**parse_camera(raw))


def sanitize_path_part(value: str) -> str:
    text = value.strip().strip("/")
    if not text:
        return "root"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)


STREAM_DIRECTORY_NAMES = {
    "color": "color",
    "depth": "depth",
    "ir": "ir",
    "left_ir": "ir_left",
    "right_ir": "ir_right",
    "left_color": "color_left",
    "right_color": "color_right",
}
IMAGE_FILE_PATTERN = re.compile(r"^image_(\d+)\.jpg$", re.IGNORECASE)


def image_stream_name(topic: str) -> str:
    """Return the stable image directory name represented by a ROS topic."""
    parts = [part for part in topic.strip().split("/") if part]
    for part in reversed(parts):
        if part in STREAM_DIRECTORY_NAMES:
            return STREAM_DIRECTORY_NAMES[part]
    for index, part in enumerate(parts):
        if part.startswith("image") and index > 0:
            return sanitize_path_part(parts[index - 1])
    return sanitize_path_part(parts[-1] if parts else topic)


class ImagePathSequence:
    """Allocate non-overwriting paths per camera and stream."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self._next_indices: Dict[tuple[str, str], int] = {}

    def next_path(self, topic: str, camera_name: str) -> Path:
        stream_name = image_stream_name(topic)
        safe_camera_name = sanitize_path_part(camera_name or "unknown_camera")
        sequence_key = (safe_camera_name, stream_name)
        stream_dir = ensure_dir(self.output_root / safe_camera_name / stream_name)
        next_index = self._next_indices.get(sequence_key)
        if next_index is None:
            existing_indices = [
                int(match.group(1))
                for path in stream_dir.iterdir()
                if path.is_file() and (match := IMAGE_FILE_PATTERN.match(path.name))
            ]
            next_index = max(existing_indices, default=0) + 1
        target = stream_dir / f"image_{next_index:04d}.jpg"
        while target.exists():
            next_index += 1
            target = stream_dir / f"image_{next_index:04d}.jpg"
        self._next_indices[sequence_key] = next_index + 1
        return target


def expand_camera_template(value: str, camera_name: str) -> str:
    return value.replace("{camera}", camera_name).replace("${camera}", camera_name)


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


def build_upgrade_command(
    *,
    ros_version: str,
    preset_path: Path,
    serial_number: str,
    usb_port: str,
    device_ip: str,
    device_port: str,
    sdk_log_level: str,
) -> List[str]:
    command = (
        ["rosrun", "orbbec_camera", "firmware_update_tool"]
        if ros_version == "1"
        else ["ros2", "run", "orbbec_camera", "firmware_update_tool", "--"]
    )
    if serial_number:
        command.extend(["--serial_number", serial_number])
    if usb_port:
        command.extend(["--usb_port", usb_port])
    if device_ip:
        command.extend(["--device_ip", device_ip])
    if device_port:
        command.extend(["--device_port", device_port])
    command.extend(["--preset_path", str(preset_path)])
    if sdk_log_level:
        command.extend(["--sdk_log_level", sdk_log_level])
    return command


class StatusLogger:
    def __init__(self, events: Optional[EventWriter] = None) -> None:
        self.events = events

    def __call__(self, message: str, *, event: str = "log", **fields: Any) -> None:
        print(f"[{timestamp()}] {message}", flush=True)
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


class LaunchSession:
    def __init__(
        self,
        *,
        camera_name: str,
        command: List[str],
        work_dir: Path,
        env: Dict[str, str],
        log_file: Path,
        emit: StatusLogger,
    ) -> None:
        self.camera_name = camera_name
        self.command = command
        self.work_dir = work_dir
        self.env = env
        self.log_file = log_file
        self.emit = emit
        self.process: Optional[subprocess.Popen[str]] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._lines: deque[str] = deque(maxlen=300)
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError(f"launch for {self.camera_name} is already running")
        ensure_dir(self.log_file.parent)
        log_stream = self.log_file.open("w", encoding="utf-8", errors="replace")
        self.process = subprocess.Popen(
            self.command,
            cwd=self.work_dir,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        def reader() -> None:
            try:
                assert self.process is not None
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    log_stream.write(line)
                    log_stream.flush()
                    with self._lock:
                        self._lines.append(line.rstrip("\n"))
            finally:
                log_stream.close()

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()

    def poll(self) -> Optional[int]:
        if self.process is None:
            return None
        return self.process.poll()

    def assert_running(self) -> None:
        code = self.poll()
        if code is not None:
            raise RuntimeError(
                f"launch for {self.camera_name} exited unexpectedly with code {code}"
            )

    def has_log_substring(self, text: str) -> bool:
        with self._lock:
            if any(text in line for line in self._lines):
                return True

        # DEBUG startup output can evict an earlier match from the bounded
        # in-memory buffer before the polling loop observes it. The reader
        # flushes every line to disk, so fall back to the complete launch log.
        try:
            with self.log_file.open("r", encoding="utf-8", errors="replace") as log_stream:
                return any(text in line for line in log_stream)
        except OSError:
            # The log may be temporarily unavailable while the launch is
            # starting. Let the caller retry until its existing timeout.
            return False

    def stop(self, timeout: float = 10.0) -> None:
        if self.process is None or self.process.poll() is not None:
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
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)


def wait_for_launch_log(
    *,
    session: LaunchSession,
    expected_text: str,
    timeout: float,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.assert_running()
        if session.has_log_substring(expected_text):
            return True, f"found launch log: {expected_text}"
        time.sleep(0.1)
    return False, f"launch log did not contain '{expected_text}' within {timeout:.1f}s"


class RosImageHarness:
    def __init__(self, ros_version: str, node_name: str, queue_size: int) -> None:
        self.ros_version = str(ros_version)
        self.node_name = node_name
        self.queue_size = queue_size
        self._rclpy = None
        self._rospy = None
        self.node = None
        self.subscriptions = []
        self.image_type = None

    def __enter__(self) -> "RosImageHarness":
        if self.ros_version == "2":
            try:
                import rclpy
                from sensor_msgs.msg import Image
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS2 Python modules. Source ROS2 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rclpy.init(args=None)
            self._rclpy = rclpy
            self.node = rclpy.create_node(self.node_name)
            self.image_type = Image
        else:
            try:
                import rospy
                from sensor_msgs.msg import Image
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS1 Python modules. Source ROS1 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rospy.init_node(self.node_name, anonymous=True, disable_signals=True)
            self._rospy = rospy
            self.image_type = Image
        return self

    def create_image_subscription(self, topic: str, callback):
        if self.ros_version == "2":
            sub = self.node.create_subscription(self.image_type, topic, callback, self.queue_size)
        else:
            sub = self._rospy.Subscriber(topic, self.image_type, callback, queue_size=self.queue_size)
        self.subscriptions.append(sub)
        return sub

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


def camera_for_topic(topic: str, camera_names: List[str]) -> Optional[str]:
    normalized_topic = "/" + topic.strip("/")
    for camera_name in sorted(camera_names, key=len, reverse=True):
        camera_prefix = "/" + camera_name.strip("/")
        if normalized_topic.startswith(camera_prefix + "/"):
            return camera_name
    return None


def discover_image_topics(
    *,
    harness: RosImageHarness,
    camera_names: List[str],
    sessions: List[LaunchSession],
    timeout: float,
    settle_seconds: float = 1.0,
) -> tuple[List[str], Dict[str, str]]:
    """Discover stable raw image topics within every configured camera namespace."""
    image_types = {"sensor_msgs/msg/Image", "sensor_msgs/Image"}
    deadline = time.monotonic() + timeout
    last_topics: List[str] = []
    unchanged_since: Optional[float] = None
    latest_topic_cameras: Dict[str, str] = {}
    while time.monotonic() < deadline:
        for session in sessions:
            session.assert_running()
        topic_cameras: Dict[str, str] = {}
        for topic, type_names in harness.get_topic_names_and_types().items():
            if not any(type_name in image_types for type_name in type_names):
                continue
            camera_name = camera_for_topic(topic, camera_names)
            if camera_name is not None:
                topic_cameras[topic] = camera_name
        topics = sorted(topic_cameras)
        now = time.monotonic()
        if topics != last_topics:
            last_topics = topics
            latest_topic_cameras = topic_cameras
            unchanged_since = now
        covered_cameras = set(latest_topic_cameras.values())
        if (
            topics
            and covered_cameras == set(camera_names)
            and unchanged_since is not None
            and now - unchanged_since >= settle_seconds
        ):
            return topics, latest_topic_cameras
        harness.spin_once(0.1)
    missing = sorted(set(camera_names) - set(latest_topic_cameras.values()))
    detail = f"; no image topics for: {', '.join(missing)}" if missing else ""
    raise TimeoutError(f"image topic discovery timed out after {timeout:.1f}s{detail}")


def _valid_image(message: Any) -> bool:
    width = int(getattr(message, "width", 0) or 0)
    height = int(getattr(message, "height", 0) or 0)
    data = getattr(message, "data", b"") or b""
    return width > 0 and height > 0 and len(data) > 0


class ImageCaptureMonitor:
    def __init__(
        self,
        *,
        harness: RosImageHarness,
        topics: List[str],
        topic_cameras: Dict[str, str],
        output_root: Path,
        save_images_count: int,
        jpg_quality: int,
    ) -> None:
        self.harness = harness
        self.topics = topics
        self.topic_cameras = topic_cameras
        self.output_root = output_root
        self.save_images_count = save_images_count
        self.jpg_quality = jpg_quality
        self.state: Dict[str, Dict[str, Any]] = {}
        self.subscriptions = []
        self._bridge = None
        self._cv2 = None
        self._image_paths = ImagePathSequence(output_root)
        for topic in topics:
            self.state[topic] = {
                "message_count": 0,
                "first_message_at": None,
                "last_message_at": None,
                "width": 0,
                "height": 0,
                "encoding": "",
                "data_size": 0,
                "saved_files": [],
            }
            self.subscriptions.append(
                self.harness.create_image_subscription(
                    topic,
                    lambda msg, topic_name=topic: self._on_message(topic_name, msg),
                )
            )

    def _ensure_cv_tools(self):
        if self._bridge is not None and self._cv2 is not None:
            return self._bridge, self._cv2
        try:
            import cv2
            from cv_bridge import CvBridge
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "saving JPG images requires cv_bridge and OpenCV Python modules. "
                "Source the camera driver environment or set --save-image-count 0 "
                f"to disable image saving. Original error: {exc}"
            ) from exc
        self._bridge = CvBridge()
        self._cv2 = cv2
        return self._bridge, self._cv2

    def _write_jpg(self, topic_name: str, message: Any, target_path: Path) -> Dict[str, Any]:
        ensure_dir(target_path.parent)
        bridge, cv2 = self._ensure_cv_tools()
        encoding = str(getattr(message, "encoding", "") or "")
        image = bridge.imgmsg_to_cv2(message, desired_encoding="passthrough")
        if encoding.lower() == "rgb8":
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif encoding.lower() in {"mono16", "16uc1"}:
            image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX)
            image = image.astype("uint8")
        if not cv2.imwrite(
            str(target_path),
            image,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.jpg_quality],
        ):
            raise RuntimeError(f"failed to write JPG image: {target_path}")
        return {
            "width": int(getattr(message, "width", 0) or 0),
            "height": int(getattr(message, "height", 0) or 0),
            "encoding": str(getattr(message, "encoding", "") or ""),
        }

    def _on_message(self, topic_name: str, message: Any) -> None:
        now = time.monotonic()
        item = self.state[topic_name]
        item["message_count"] += 1
        item["last_message_at"] = now
        item["width"] = int(getattr(message, "width", 0) or 0)
        item["height"] = int(getattr(message, "height", 0) or 0)
        item["encoding"] = str(getattr(message, "encoding", "") or "")
        item["data_size"] = len(getattr(message, "data", b"") or b"")
        if _valid_image(message) and item["first_message_at"] is None:
            item["first_message_at"] = now
        if self.save_images_count <= 0 or not _valid_image(message):
            return
        saved_files = item["saved_files"]
        if len(saved_files) >= self.save_images_count:
            return
        camera_name = self.topic_cameras.get(topic_name, "unknown_camera")
        target_path = self._image_paths.next_path(topic_name, camera_name)
        metadata = self._write_jpg(topic_name, message, target_path)
        saved_files.append(str(target_path))
        item["metadata"] = metadata

    def complete(self) -> bool:
        for item in self.state.values():
            if item["first_message_at"] is None:
                return False
            if self.save_images_count > 0 and len(item["saved_files"]) < self.save_images_count:
                return False
        return True

    def snapshot(self) -> List[Dict[str, Any]]:
        rows = []
        for topic, item in self.state.items():
            rows.append(
                {
                    "name": topic,
                    "topic": topic,
                    "camera": self.topic_cameras.get(topic, ""),
                    "topic_kind": "raw",
                    "message_count": item["message_count"],
                    "width": item["width"],
                    "height": item["height"],
                    "data_size": item["data_size"],
                    "encoding": item["encoding"],
                    "format": "",
                    "saved_count": len(item["saved_files"]),
                    "expected_count": self.save_images_count,
                    "files": list(item["saved_files"]),
                }
            )
        return rows

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


def wait_for_images(
    *,
    sessions: List[LaunchSession],
    harness: RosImageHarness,
    topics: List[str],
    topic_cameras: Dict[str, str],
    output_root: Path,
    save_images_count: int,
    timeout: float,
    jpg_quality: int,
) -> tuple[bool, List[Dict[str, Any]], str]:
    monitor = ImageCaptureMonitor(
        harness=harness,
        topics=topics,
        topic_cameras=topic_cameras,
        output_root=output_root,
        save_images_count=save_images_count,
        jpg_quality=jpg_quality,
    )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            for session in sessions:
                session.assert_running()
            harness.spin_once(0.1)
            if monitor.complete():
                if save_images_count > 0:
                    total = sum(row["saved_count"] for row in monitor.snapshot())
                    return True, monitor.snapshot(), f"received streams and saved {total} JPG image(s)"
                return True, monitor.snapshot(), "received all image streams"
        return False, monitor.snapshot(), f"image streams were not complete within {timeout:.1f}s"
    finally:
        monitor.close()


def run_command_to_log(command: List[str], env: Dict[str, str], work_dir: Path, log_file: Path) -> int:
    ensure_dir(log_file.parent)
    with log_file.open("w", encoding="utf-8", errors="replace") as stream:
        stream.write("$ " + " ".join(shlex.quote(item) for item in command) + "\n")
        stream.flush()
        process = subprocess.Popen(
            command,
            cwd=work_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stream.write(line)
                stream.flush()
            return process.wait()
        except KeyboardInterrupt:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            raise


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def build_summary(result: Dict[str, Any]) -> str:
    tests = result.get("tests", [])
    status_counts: Dict[str, int] = {}
    failed_tests = []
    for test in tests:
        status = str(test.get("status", "unknown") or "unknown")
        if result.get("status") == "interrupted" and status == "running":
            status = "interrupted"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "passed":
            failed_tests.append(test)
    planned_tests = result.get("planned_tests", "duration mode")
    lines = [
        "# Preset Upgrade Stress Test",
        "",
        *test_environment_markdown(result.get("environment", {})),
        "## Result",
        "",
        f"- Status: {result.get('status', '')}",
        f"- Tool version: {result.get('tool_version', '')}",
        f"- Passed tests: {result.get('passed_tests', 0)}",
        f"- Planned tests: {planned_tests}",
        f"- Completed tests: {len(tests)}",
        f"- Failed tests: {len(failed_tests)}",
        f"- Elapsed seconds: {float(result.get('elapsed_seconds', 0.0) or 0.0):.1f}",
        f"- JPG images per topic per test: {result.get('save_image_count', 0)}",
        "",
        "## Cameras",
        "",
    ]
    cameras = result.get("cameras") or [{"name": result.get("camera_name", "")}]
    for camera in cameras:
        label = str(camera.get("name", ""))
        selectors = []
        if camera.get("usb_port"):
            selectors.append(f"usb-port={camera['usb_port']}")
        if camera.get("serial_number"):
            selectors.append(f"serial-number={camera['serial_number']}")
        if selectors:
            label += f" ({', '.join(selectors)})"
        lines.append(f"- {label}")
    lines.extend(["", "## Presets", ""])
    for preset in result.get("presets", []):
        lines.append(f"- {preset.get('key')}: `{preset.get('name')}` from `{preset.get('path')}`")
    lines.extend(["", "## Image Topics", ""])
    for topic in result.get("image_topics", []):
        lines.append(f"- {topic}")
    if result.get("error"):
        lines.extend(["", "## Error", "", str(result["error"])])

    lines.extend(["", "## Test Statistics", ""])
    if status_counts:
        for status in sorted(status_counts):
            lines.append(f"- {status}: {status_counts[status]}")
    else:
        lines.append("- No tests recorded")

    lines.extend(["", "## Failures", ""])
    if not failed_tests:
        lines.append("- None")
    else:
        for test in failed_tests:
            status = str(test.get("status", "") or "")
            if result.get("status") == "interrupted" and status == "running":
                status = "interrupted"
            preset_path = Path(str(test.get("preset_path", ""))).name
            test_index = int(test.get("test_index") or test.get("round", 0) or 0)
            lines.append(
                f"- test_{test_index:04d}: "
                f"{status} ({test.get('preset_name', '')}, {preset_path})"
            )
            if test.get("message"):
                lines.append(f"  {test['message']}")
            if test.get("preset_log_message") and test.get("status") != "passed":
                lines.append(f"  - preset log: {test['preset_log_message']}")
            if test.get("upgrade_returncode") not in (None, 0):
                lines.append(f"  - upgrade returncode: {test['upgrade_returncode']}")
            for upgrade_result in test.get("upgrades", []):
                if upgrade_result.get("returncode") in (None, 0):
                    continue
                lines.append(
                    f"  - {upgrade_result.get('camera', '')}: "
                    f"upgrade returncode={upgrade_result.get('returncode')}"
                )
                if upgrade_result.get("log"):
                    lines.append(f"    - log: {upgrade_result['log']}")
            for launch_result in test.get("launches", []):
                if launch_result.get("preset_log_message") and status != "passed":
                    lines.append(
                        f"  - {launch_result.get('camera', '')}: "
                        f"{launch_result.get('preset_log_message', '')}"
                    )
                if launch_result.get("log") and status != "passed":
                    lines.append(f"    - log: {launch_result['log']}")
    return "\n".join(lines) + "\n"


def normalize_preset_specs(args) -> List[PresetSpec]:
    specs = [
        PresetSpec(
            key="preset_a",
            path=Path(args.preset_a_path).expanduser().resolve(),
            name=str(args.preset_a_name).strip(),
        ),
        PresetSpec(
            key="preset_b",
            path=Path(args.preset_b_path).expanduser().resolve(),
            name=str(args.preset_b_name).strip(),
        ),
    ]
    for spec in specs:
        if not spec.path.is_file():
            raise FileNotFoundError(f"{spec.key} file not found: {spec.path}")
        if not spec.name:
            raise ValueError(f"--{spec.key.replace('_', '-')}-name cannot be empty")
    return specs


def select_launch_file(args) -> str:
    if args.launch_file:
        return args.launch_file
    return DEFAULT_LAUNCH[args.ros_version]


def build_base_launch_args(args) -> Dict[str, str]:
    launch_args: Dict[str, str] = {
        **DEFAULT_STRESS_LAUNCH_ARGS,
        "enable_color": "true",
        "enable_depth": "true",
    }
    for raw_arg in args.launch_arg:
        key, value = parse_launch_arg(raw_arg)
        launch_args[key] = value
    return launch_args


def build_camera_launch_args(
    *,
    common_launch_args: Dict[str, str],
    camera: CameraSpec,
    preset_name: str,
) -> Dict[str, str]:
    launch_args = dict(common_launch_args)
    launch_args["camera_name"] = camera.name
    launch_args["device_preset"] = preset_name
    if camera.usb_port:
        launch_args["usb_port"] = camera.usb_port
    if camera.serial_number:
        launch_args["serial_number"] = camera.serial_number
    if camera.device_ip:
        launch_args["net_device_ip"] = camera.device_ip
    if camera.device_port:
        launch_args["net_device_port"] = camera.device_port
    if camera.config_file_path:
        launch_args["config_file_path"] = camera.config_file_path
    return launch_args


def run(args) -> int:
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    runtime_env = prepare_runtime_env(args)
    apply_python_paths(runtime_env)
    environment = collect_test_environment(args)

    cameras = [parse_camera_spec(raw) for raw in args.camera] or [
        parse_camera_spec("name=camera")
    ]

    run_count = args.run_count
    if run_count is not None and run_count <= 0:
        raise ValueError("--run-count must be > 0")
    save_images_count = int(args.save_image_count)
    if save_images_count < 0:
        raise ValueError("--save-image-count must be >= 0")
    jpg_quality = int(args.jpg_quality)
    if jpg_quality < 1 or jpg_quality > 100:
        raise ValueError("--jpg-quality must be in range 1-100")
    duration_seconds = (
        parse_duration(args.duration, 0.0) if str(args.duration or "").strip() else None
    )
    stream_timeout = parse_duration(args.stream_timeout, 30.0)
    preset_log_timeout = parse_duration(args.preset_log_timeout, 20.0)
    restart_delay = float(args.restart_delay)
    if restart_delay < 0:
        raise ValueError("--restart-delay must be >= 0")
    launch_start_interval = parse_duration(args.launch_start_interval, 2.0)

    presets = normalize_preset_specs(args)
    launch_file = select_launch_file(args)
    base_launch_args = build_base_launch_args(args)
    image_topic_templates = [topic.strip() for topic in args.image_topic if topic.strip()]
    auto_discover_image_topics = not image_topic_templates
    topics = [
        expand_camera_template(topic_template.strip(), camera.name)
        for camera in cameras
        for topic_template in image_topic_templates
        if topic_template.strip()
    ]
    topic_cameras = {
        expand_camera_template(topic_template.strip(), camera.name): camera.name
        for camera in cameras
        for topic_template in image_topic_templates
        if topic_template.strip()
    }

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_preset_upgrade")
    run_started_at = iso_now()
    results_dir = ensure_dir(
        Path(args.results_dir or (SCRIPT_DIR / "results" / run_id)).expanduser().resolve()
    )
    events = EventWriter(results_dir / "events.jsonl")
    emit = StatusLogger(events)
    result: Dict[str, Any] = {
        "status": "passed",
        "tool_version": TOOL_VERSION,
        "environment": environment,
        "ros_version": args.ros_version,
        "launch_file": launch_file,
        "launch_package": args.launch_package,
        "launch_args": base_launch_args,
        "camera_name": cameras[0].name if cameras else "",
        "cameras": [asdict(camera) for camera in cameras],
        "image_topics": topics,
        "run_count": run_count,
        "planned_tests": run_count * len(presets) if run_count is not None else "duration mode",
        "duration_limit_seconds": duration_seconds,
        "stream_timeout_seconds": stream_timeout,
        "preset_log_timeout_seconds": preset_log_timeout,
        "save_image_count": save_images_count,
        "save_image_timeout_seconds": stream_timeout,
        "jpg_quality": jpg_quality,
        "presets": [asdict(spec) | {"path": str(spec.path)} for spec in presets],
        "tests": [],
        "passed_tests": 0,
        "elapsed_seconds": 0.0,
    }

    emit("test started", event="phase", phase="starting")
    emit(f"tool version: {TOOL_VERSION}")
    emit(f"results dir: {results_dir}")
    emit(f"run count: {run_count if run_count is not None else 'duration-limited'}")
    emit(f"cameras: {', '.join(camera.name for camera in cameras)}")
    if auto_discover_image_topics:
        emit("monitor topics: auto-discover all published image streams")
    else:
        emit(f"monitor topics: {', '.join(topics)}")
    emit(f"save images per topic: {save_images_count}")
    start_monotonic = time.monotonic()
    deadline = (
        start_monotonic + duration_seconds
        if duration_seconds is not None
        else None
    )
    active_sessions: List[LaunchSession] = []
    test_index = 0

    try:
        with RosImageHarness(args.ros_version, "preset_upgrade_stress_test", args.queue_size) as harness:
            round_index = 0
            while True:
                if INTERRUPTED:
                    result["status"] = "interrupted"
                    emit(
                        "stop requested; current preset cycle completed",
                        event="phase",
                        phase="stopped-at-safe-point",
                    )
                    break
                if run_count is not None and round_index >= run_count:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                round_index += 1
                for preset in presets:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    test_index += 1
                    test_name = f"test_{test_index:04d}"
                    test_log_dir = ensure_dir(results_dir / "logs" / test_name)
                    test_image_dir = results_dir / "images"
                    test_record: Dict[str, Any] = {
                        "test_index": test_index,
                        "round": round_index,
                        "preset_key": preset.key,
                        "preset_name": preset.name,
                        "preset_path": str(preset.path),
                        "status": "running",
                        "message": "",
                        "started_at": datetime.now().isoformat(timespec="seconds"),
                        "ended_at": "",
                        "upgrades": [],
                        "launches": [],
                        "topics": [],
                        "images": [],
                    }
                    result["tests"].append(test_record)

                    for camera in cameras:
                        camera_log_dir = ensure_dir(test_log_dir / sanitize_path_part(camera.name))
                        emit(
                            f"{test_name}: upgrade {preset.name} for {camera.name}",
                            event="progress",
                            current=round_index,
                            total=run_count,
                            phase="updating",
                        )
                        upgrade_command = build_upgrade_command(
                            ros_version=args.ros_version,
                            preset_path=preset.path,
                            serial_number=camera.serial_number,
                            usb_port=camera.usb_port,
                            device_ip=camera.device_ip,
                            device_port=camera.device_port,
                            sdk_log_level=args.sdk_log_level,
                        )
                        upgrade_result = {
                            "camera": camera.name,
                            "command": upgrade_command,
                            "log": str(camera_log_dir / "upgrade.log"),
                            "returncode": None,
                        }
                        test_record["upgrades"].append(upgrade_result)
                        upgrade_env = dict(runtime_env)
                        upgrade_env["ORBBEC_LOG_DIR"] = str(ensure_dir(camera_log_dir / "sdk"))
                        upgrade_code = run_command_to_log(
                            upgrade_command,
                            upgrade_env,
                            results_dir,
                            camera_log_dir / "upgrade.log",
                        )
                        upgrade_result["returncode"] = upgrade_code
                        if upgrade_code != 0:
                            raise RuntimeError(
                                f"{camera.name}: preset upgrade failed with code {upgrade_code}"
                            )
                        if INTERRUPTED:
                            test_record["status"] = "interrupted"
                            test_record["message"] = (
                                "stop requested after current preset upgrade completed"
                            )
                            test_record["ended_at"] = datetime.now().isoformat(
                                timespec="seconds"
                            )
                            result["status"] = "interrupted"
                            emit(
                                test_record["message"],
                                event="phase",
                                phase="stopped-at-safe-point",
                            )
                            break

                    if INTERRUPTED:
                        break

                    sessions: List[LaunchSession] = []
                    expected_log = f"Loaded device preset: {preset.name}"
                    for camera in cameras:
                        camera_log_dir = ensure_dir(test_log_dir / sanitize_path_part(camera.name))
                        launch_args = build_camera_launch_args(
                            common_launch_args=base_launch_args,
                            camera=camera,
                            preset_name=preset.name,
                        )
                        launch_args["log_level"] = args.sdk_log_level
                        launch_args["log_file_name"] = f"{camera.name}.log"
                        launch_command = build_launch_command(
                            ros_version=args.ros_version,
                            launch_package=args.launch_package,
                            launch_file=launch_file,
                            launch_args=launch_args,
                        )
                        launch_env = dict(runtime_env)
                        launch_env["ORBBEC_LOG_DIR"] = str(ensure_dir(camera_log_dir / "sdk"))
                        session = LaunchSession(
                            camera_name=camera.name,
                            command=launch_command,
                            work_dir=results_dir,
                            env=launch_env,
                            log_file=camera_log_dir / f"{sanitize_path_part(camera.name)}.launch.log",
                            emit=emit,
                        )
                        sessions.append(session)
                        test_record["launches"].append(
                            {
                                "camera": camera.name,
                                "command": launch_command,
                                "launch_args": launch_args,
                                "log": str(
                                    camera_log_dir / f"{sanitize_path_part(camera.name)}.launch.log"
                                ),
                                "preset_log_message": "",
                            }
                        )
                    active_sessions = sessions

                    for index, session in enumerate(sessions):
                        emit(f"{test_name}: start launch for {session.camera_name}")
                        session.start()
                        if index < len(sessions) - 1:
                            time.sleep(launch_start_interval)

                    for index, session in enumerate(sessions):
                        ok, message = wait_for_launch_log(
                            session=session,
                            expected_text=expected_log,
                            timeout=preset_log_timeout,
                        )
                        test_record["launches"][index]["preset_log_message"] = message
                        if not ok:
                            raise RuntimeError(f"{session.camera_name}: {message}")

                    if auto_discover_image_topics:
                        topics, topic_cameras = discover_image_topics(
                            harness=harness,
                            camera_names=[camera.name for camera in cameras],
                            sessions=sessions,
                            timeout=stream_timeout,
                        )
                        result["image_topics"] = sorted(
                            set(result["image_topics"]) | set(topics)
                        )
                        emit(f"{test_name}: discovered image topics: {', '.join(topics)}")

                    ok, image_snapshot, image_message = wait_for_images(
                        sessions=sessions,
                        harness=harness,
                        topics=topics,
                        topic_cameras=topic_cameras,
                        output_root=test_image_dir,
                        save_images_count=save_images_count,
                        timeout=stream_timeout,
                        jpg_quality=jpg_quality,
                    )
                    test_record["topics"] = [
                        {
                            "name": row.get("topic", row.get("name", "")),
                            "topic_kind": row.get("topic_kind", "raw"),
                            "message_count": row.get("message_count", 0),
                            "width": row.get("width", 0),
                            "height": row.get("height", 0),
                            "data_size": row.get("data_size", 0),
                        }
                        for row in image_snapshot
                    ]
                    test_record["images"] = image_snapshot
                    test_record["message"] = image_message
                    if not ok:
                        raise RuntimeError(image_message)

                    for session in reversed(sessions):
                        session.stop()
                    active_sessions = []
                    test_record["status"] = "passed"
                    test_record["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    result["passed_tests"] += 1
                    emit(
                        f"{test_name}: passed, preset={preset.name}",
                        event="progress",
                        current=round_index,
                        total=run_count,
                        phase="completed-cycle",
                    )
                    if INTERRUPTED:
                        result["status"] = "interrupted"
                        emit(
                            "stop requested; current preset update completed",
                            event="phase",
                            phase="stopped-at-safe-point",
                        )
                        break
                    if restart_delay > 0:
                        time.sleep(restart_delay)
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        if result["tests"] and result["tests"][-1].get("status") == "running":
            result["tests"][-1]["status"] = "interrupted"
            result["tests"][-1]["message"] = "interrupted by user"
        emit("test interrupted by user")
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            result["status"] = "interrupted"
            if result["tests"] and result["tests"][-1].get("status") == "running":
                result["tests"][-1]["status"] = "interrupted"
                result["tests"][-1]["message"] = "interrupted by user"
            emit("test interrupted by user")
        else:
            result["status"] = "failed"
            result["error"] = str(exc)
            if result["tests"]:
                result["tests"][-1]["status"] = "failed"
                result["tests"][-1]["message"] = str(exc)
            emit(f"test failed: {exc}")
    finally:
        if active_sessions:
            emit("stop launches")
            for session in reversed(active_sessions):
                session.stop()
        for test in result.get("tests", []):
            if not test.get("ended_at"):
                test["ended_at"] = datetime.now().isoformat(timespec="seconds")
        result["elapsed_seconds"] = time.monotonic() - start_monotonic
        (results_dir / "summary.md").write_text(build_summary(result), encoding="utf-8")
        emit(
            f"test finished with status {result['status']}",
            event="completed",
            status=result["status"],
        )
        payload = contract_result(
            test_id=TEST_ID,
            run_id=run_id,
            started_at=run_started_at,
            ended_at=iso_now(),
            request=namespace_request(args),
            details=result,
            summary={
                "passed_tests": result["passed_tests"],
                "completed_tests": len(result["tests"]),
            },
            artifacts=artifact_list(results_dir),
        )
        atomic_write_json(results_dir / "result.json", payload)
        signal.signal(signal.SIGINT, previous_sigint_handler)

    if result["status"] == "passed":
        emit(f"test finished successfully, passed tests={result['passed_tests']}")
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Alternately update Orbbec optional depth presets and verify launch streams.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py "
            "--ros-version 2 --driver-setup /path/to/install/setup.bash --run-count 1\n\n"
            "  python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py "
            "--run-count 1 --camera name=camera_01,usb-port=2-1 "
            "--camera name=camera_02,usb-port=2-3\n\n"
            "  python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py "
            "--preset-a-path /path/a.bin --preset-a-name 'K High Confidence' "
            "--preset-b-path /path/b.bin --preset-b-name 'K High Accuracy'\n"
        ),
    )
    parser.add_argument("--ros-version", choices=("1", "2"), default=os.environ.get("ROS_VERSION", "2"))
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument("--driver-setup", default=os.environ.get("ORBBEC_CAMERA_SETUP", ""))
    parser.add_argument("--launch-package", default="orbbec_camera")
    parser.add_argument("--launch-file", default="", help="Launch filename or absolute/relative launch path")
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra launch arg, KEY=VALUE or KEY:=VALUE")
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        help=(
            "Camera launch arguments as comma-separated KEY=VALUE fields. "
            "Supported keys: name, serial-number, usb-port, device-ip, "
            "device-port, config-file-path. Repeat for multiple cameras."
        ),
    )
    parser.add_argument("--preset-a-path", default=str(DEFAULT_PRESET_A_PATH))
    parser.add_argument("--preset-a-name", default="K High Confidence")
    parser.add_argument("--preset-b-path", default=str(DEFAULT_PRESET_B_PATH))
    parser.add_argument("--preset-b-name", default="K High Accuracy")
    parser.add_argument("--run-count", type=int, default=None, help="Optional maximum preset rounds")
    parser.add_argument("--duration", default="300", help="Maximum wall time; supports 300, 15m, 2h")
    parser.add_argument("--stream-timeout", default="30", help="Max wait time for image streams per preset")
    parser.add_argument("--preset-log-timeout", default="20", help="Max wait time for Loaded device preset log")
    parser.add_argument("--save-image-count", type=int, default=1, help="Images to save per topic; 0 disables saving")
    parser.add_argument("--jpg-quality", type=int, default=95, help="JPG quality, 1-100")
    parser.add_argument("--restart-delay", default="2", help="Delay seconds after stopping launch")
    parser.add_argument(
        "--launch-start-interval",
        default="2",
        help="Delay in seconds between starting each camera launch (default: 2)",
    )
    parser.add_argument(
        "--sdk-log-level",
        choices=("debug", "info", "warn", "error", "fatal", "off", "none"),
        default="debug",
    )
    parser.add_argument(
        "--image-topic",
        action="append",
        default=[],
        help=(
            "Image topic to monitor/save; can repeat and supports {camera}. "
            "When omitted, all published image streams under each camera are discovered."
        ),
    )
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
