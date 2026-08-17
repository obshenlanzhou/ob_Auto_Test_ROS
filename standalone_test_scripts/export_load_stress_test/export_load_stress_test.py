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
import time
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
    install_terminal_log,
    iso_now,
    namespace_request,
    parse_camera,
    test_environment_markdown,
)
from _sensor_artifacts import (
    capture_sensor_artifacts,
    discover_sensor_topics,
    expand_topic_templates,
)

ENV_READY_VAR = "EXPORT_LOAD_STRESS_TEST_ENV_READY"
INTERRUPTED = False
TOOL_VERSION = "1.9"
TEST_ID = "export_load_stress_test"
DEFAULT_STRESS_LAUNCH_ARGS = {
    "enable_heartbeat": "true",
    "enable_firmware_log": "true",
}
DEFAULT_CONFIG_JSONS = [
    Path(__file__).resolve().parent / "config" / "Gemini_336L_1.json",
    Path(__file__).resolve().parent / "config" / "Gemini_336L_2.json",
]


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
    raise KeyboardInterrupt


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


def expand_camera_template(value: str, camera_name: str) -> str:
    return value.replace("{camera}", camera_name).replace("${camera}", camera_name)


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
IMAGE_FILE_PATTERN = re.compile(r"^image_(\d+)\.(?:png|jpg)$", re.IGNORECASE)


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

    def next_path(self, topic: str, camera_name: str, suffix: str = ".png") -> Path:
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
        target = stream_dir / f"image_{next_index:04d}{suffix}"
        while target.exists():
            next_index += 1
            target = stream_dir / f"image_{next_index:04d}{suffix}"
        self._next_indices[sequence_key] = next_index + 1
        return target


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


def _split_ros_type(type_name: str) -> tuple[str, str]:
    parts = type_name.split("/")
    if len(parts) == 3:
        return parts[0], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"Unsupported ROS type format: {type_name}")


def _ros1_package_name(package_name: str) -> str:
    if package_name == "orbbec_camera_msgs":
        return "orbbec_camera"
    return package_name


def resolve_service_type(type_name: str, ros_version: str):
    import importlib

    package_name, service_name = _split_ros_type(type_name)
    if str(ros_version) == "1":
        package_name = _ros1_package_name(package_name)
    module = importlib.import_module(f"{package_name}.srv")
    return getattr(module, service_name)


def populate_message_fields(message: Any, values: Dict[str, Any]) -> Any:
    for key, value in values.items():
        current = getattr(message, key)
        if isinstance(value, dict) and not isinstance(current, (str, bytes)):
            populate_message_fields(current, value)
            setattr(message, key, current)
        else:
            setattr(message, key, value)
    return message


class LaunchSession:
    def __init__(
        self,
        *,
        camera_name: str,
        command: List[str],
        work_dir: Path,
        env: Dict[str, str],
        log_path: Path,
        emit: StatusLogger,
    ) -> None:
        self.camera_name = camera_name
        self.command = command
        self.work_dir = work_dir
        self.env = env
        self.log_path = log_path
        self.emit = emit
        self.process: Optional[subprocess.Popen[str]] = None
        self._log_handle = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError(f"launch for {self.camera_name} is already running")
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
            raise RuntimeError(
                f"launch for {self.camera_name} exited unexpectedly with code {code}"
            )

    def stop(self, timeout: float = 10.0) -> None:
        if self.process is None or self.process.poll() is not None:
            self._close_log()
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.emit(f"{self.camera_name}: launch did not stop after SIGINT, sending SIGTERM")
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.emit(
                    f"{self.camera_name}: launch did not stop after SIGTERM, sending SIGKILL"
                )
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


class RosHarness:
    def __init__(self, ros_version: str, node_name: str, queue_size: int) -> None:
        self.ros_version = str(ros_version)
        self.node_name = node_name
        self.queue_size = queue_size
        self._rclpy = None
        self._rospy = None
        self.node = None
        self._sensor_qos = None
        self.subscriptions = []
        self.message_types = {}

    def __enter__(self) -> "RosHarness":
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

    def create_image_subscription(self, topic: str, callback, topic_kind: Optional[str] = None):
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

    def spin_until(self, predicate, timeout: float, description: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.spin_once(0.1)
        raise TimeoutError(f"Timed out waiting for {description}")

    def call_service(
        self,
        service_name: str,
        service_type_name: str,
        request_data: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ):
        srv_type = resolve_service_type(service_type_name, self.ros_version)
        if self.ros_version == "1":
            self._rospy.wait_for_service(service_name, timeout=timeout)
            proxy = self._rospy.ServiceProxy(service_name, srv_type)
            request = srv_type._request_class()
            if request_data:
                populate_message_fields(request, request_data)
            return proxy(request)

        client = self.node.create_client(srv_type, service_name)
        try:
            self.spin_until(
                lambda: client.wait_for_service(timeout_sec=0.1),
                timeout,
                f"service {service_name}",
            )
            request = srv_type.Request()
            if request_data:
                populate_message_fields(request, request_data)
            future = client.call_async(request)
            self.spin_until(lambda: future.done(), timeout, f"response from {service_name}")
            return future.result()
        finally:
            self.node.destroy_client(client)

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
    harness: RosHarness,
    camera_names: List[str],
    sessions: List[LaunchSession],
    timeout: float,
    settle_seconds: float = 1.0,
) -> tuple[List[str], Dict[str, str]]:
    """Discover stable raw image topics within every configured camera namespace."""
    deadline = time.monotonic() + timeout
    last_topics: List[str] = []
    unchanged_since: Optional[float] = None
    latest_topic_cameras: Dict[str, str] = {}
    while time.monotonic() < deadline:
        for session in sessions:
            session.assert_running()
        topic_cameras: Dict[str, str] = {}
        for topic, type_names in harness.get_topic_names_and_types().items():
            if not any(_is_image_type(type_name) for type_name in type_names):
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


class StableImageMonitor:
    def __init__(self, harness: RosHarness, topics: List[str]) -> None:
        self.harness = harness
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
        if item["first_message_at"] is None:
            item["first_message_at"] = now

    def all_streams_detected(self) -> bool:
        return all(item["first_message_at"] is not None for item in self.state.values())

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

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


class ImageSaver:
    def __init__(
        self,
        *,
        harness: RosHarness,
        topics: List[str],
        topic_cameras: Dict[str, str],
        output_root: Path,
        count_per_topic: int,
    ) -> None:
        self.harness = harness
        self.topics = topics
        self.topic_cameras = topic_cameras
        self.output_root = output_root
        self.count_per_topic = count_per_topic
        self.saved: Dict[str, List[str]] = {topic: [] for topic in topics}
        self.metadata: Dict[str, Dict[str, Any]] = {topic: {} for topic in topics}
        self.subscriptions = []
        self._bridge = None
        self._cv2 = None
        self._image_paths = ImagePathSequence(output_root)

        for topic in topics:
            topic_kind = self.harness.resolve_image_topic_kind(topic)
            self.metadata[topic]["topic_kind"] = topic_kind
            self.subscriptions.append(
                self.harness.create_image_subscription(
                    topic,
                    lambda msg, topic_name=topic: self._on_message(topic_name, msg),
                    topic_kind=topic_kind,
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
                "saving raw images as PNG requires cv_bridge and OpenCV Python modules. "
                "Source the camera driver environment or set --save-image-count 0 "
                f"to disable image saving. Original error: {exc}"
            ) from exc
        self._bridge = CvBridge()
        self._cv2 = cv2
        return self._bridge, self._cv2

    def _target_path(self, topic_name: str) -> Path:
        camera_name = self.topic_cameras.get(topic_name, "unknown_camera")
        topic_kind = self.metadata[topic_name].get("topic_kind", "raw")
        suffix = ".jpg" if topic_kind == "compressed" else ".png"
        return self._image_paths.next_path(topic_name, camera_name, suffix)

    def _write_image(self, topic_name: str, message: Any, target_path: Path) -> None:
        topic_kind = self.metadata[topic_name].get("topic_kind", "raw")
        ensure_dir(target_path.parent)
        if topic_kind == "compressed":
            target_path.write_bytes(bytes(getattr(message, "data", b"") or b""))
            return

        bridge, cv2 = self._ensure_cv_tools()
        encoding = str(getattr(message, "encoding", "") or "")
        image = bridge.imgmsg_to_cv2(message, desired_encoding="passthrough")
        if encoding.lower() == "rgb8":
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif encoding.lower() == "rgba8":
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA)
        if not cv2.imwrite(
            str(target_path), image, [int(cv2.IMWRITE_PNG_COMPRESSION), 1]
        ):
            raise RuntimeError(f"failed to write PNG image: {target_path}")

    def _on_message(self, topic_name: str, message: Any) -> None:
        saved_files = self.saved[topic_name]
        if len(saved_files) >= self.count_per_topic:
            return
        target_path = self._target_path(topic_name)
        self._write_image(topic_name, message, target_path)
        saved_files.append(str(target_path))
        self.metadata[topic_name].update(
            {
                "width": int(getattr(message, "width", 0) or 0),
                "height": int(getattr(message, "height", 0) or 0),
                "encoding": str(getattr(message, "encoding", "") or ""),
                "format": str(getattr(message, "format", "") or ""),
            }
        )

    def complete(self) -> bool:
        return all(len(files) >= self.count_per_topic for files in self.saved.values())

    def snapshot(self) -> List[Dict[str, Any]]:
        rows = []
        for topic_name, files in self.saved.items():
            rows.append(
                {
                    "topic": topic_name,
                    "camera": self.topic_cameras.get(topic_name, ""),
                    "saved_count": len(files),
                    "expected_count": self.count_per_topic,
                    "files": list(files),
                    **self.metadata.get(topic_name, {}),
                }
            )
        return rows

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


def save_images(
    *,
    sessions: List[LaunchSession],
    harness: RosHarness,
    topics: List[str],
    topic_cameras: Dict[str, str],
    output_root: Path,
    count_per_topic: int,
    timeout: float,
    emit: StatusLogger,
) -> tuple[bool, List[Dict[str, Any]], str]:
    if count_per_topic <= 0:
        return True, [], "image saving disabled"
    saver = ImageSaver(
        harness=harness,
        topics=topics,
        topic_cameras=topic_cameras,
        output_root=output_root,
        count_per_topic=count_per_topic,
    )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            for session in sessions:
                session.assert_running()
            harness.spin_once(0.1)
            if saver.complete():
                snapshot = saver.snapshot()
                saved_count = sum(item["saved_count"] for item in snapshot)
                return True, snapshot, f"saved {saved_count} image files"
        return (
            False,
            saver.snapshot(),
            f"did not save {count_per_topic} image(s) per topic within {timeout:.1f}s",
        )
    finally:
        saver.close()


def wait_for_stable_streams(
    *,
    sessions: List[LaunchSession],
    harness: RosHarness,
    topics: List[str],
    stable_seconds: float,
    timeout: float,
    max_gap_seconds: float,
    emit: StatusLogger,
) -> tuple[bool, List[Dict[str, Any]], str]:
    monitor = StableImageMonitor(harness, topics)
    deadline = time.monotonic() + timeout
    streams_detected_logged = False
    try:
        while time.monotonic() < deadline:
            for session in sessions:
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


def load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def normalize_config_paths(paths: List[str]) -> List[Path]:
    raw_paths = paths or [str(path) for path in DEFAULT_CONFIG_JSONS]
    normalized = []
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"config JSON not found: {path}")
        payload = load_json_file(path)
        if "parameters" not in payload:
            raise ValueError(f"config JSON missing 'parameters': {path}")
        normalized.append(path)
    if len(normalized) < 2:
        raise ValueError("at least two --config-json files are required")
    return normalized


def _path_label(path: str, expected: Any, actual: Any) -> Dict[str, Any]:
    label: Dict[str, Any] = {}
    marker = ".post_processing_filter["
    if marker not in path:
        return label
    prefix, rest = path.split(marker, 1)
    index_text = rest.split("]", 1)[0]
    try:
        index = int(index_text)
    except ValueError:
        return label
    filter_payload = {}
    if isinstance(expected, dict) and expected.get("filter_name"):
        filter_payload = expected
    elif isinstance(actual, dict) and actual.get("filter_name"):
        filter_payload = actual
    else:
        expected_filters = expected if isinstance(expected, list) else []
        actual_filters = actual if isinstance(actual, list) else []
        if 0 <= index < len(expected_filters) and isinstance(expected_filters[index], dict):
            filter_payload = expected_filters[index]
        elif 0 <= index < len(actual_filters) and isinstance(actual_filters[index], dict):
            filter_payload = actual_filters[index]
    filter_name = filter_payload.get("filter_name")
    if filter_name:
        label["filter_name"] = filter_name
        label["label"] = f"{prefix}.post_processing_filter[{index}] ({filter_name})"
    return label


def compare_json_values(
    expected: Any,
    actual: Any,
    path: str = "parameters",
    parent_expected: Any = None,
    parent_actual: Any = None,
) -> List[Dict[str, Any]]:
    diffs: List[Dict[str, Any]] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        expected_keys = set(expected)
        actual_keys = set(actual)
        for key in sorted(expected_keys.difference(actual_keys)):
            diff_path = f"{path}.{key}"
            diff = {"path": diff_path, "type": "missing", "expected": expected[key]}
            diff.update(_path_label(diff_path, parent_expected, parent_actual))
            diffs.append(diff)
        for key in sorted(actual_keys.difference(expected_keys)):
            diff_path = f"{path}.{key}"
            diff = {"path": diff_path, "type": "extra", "actual": actual[key]}
            diff.update(_path_label(diff_path, parent_expected, parent_actual))
            diffs.append(diff)
        for key in sorted(expected_keys.intersection(actual_keys)):
            diffs.extend(
                compare_json_values(
                    expected[key],
                    actual[key],
                    f"{path}.{key}",
                    parent_expected=expected,
                    parent_actual=actual,
                )
            )
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        max_length = max(len(expected), len(actual))
        for index in range(max_length):
            item_path = f"{path}[{index}]"
            if index >= len(expected):
                diff = {"path": item_path, "type": "extra", "actual": actual[index]}
                diff.update(_path_label(item_path, expected, actual))
                diffs.append(diff)
            elif index >= len(actual):
                diff = {"path": item_path, "type": "missing", "expected": expected[index]}
                diff.update(_path_label(item_path, expected, actual))
                diffs.append(diff)
            else:
                diffs.extend(
                    compare_json_values(
                        expected[index],
                        actual[index],
                        item_path,
                        parent_expected=expected,
                        parent_actual=actual,
                    )
                )
        return diffs
    if expected != actual:
        diff = {
            "path": path,
            "type": "value_mismatch",
            "expected": expected,
            "actual": actual,
        }
        diff.update(_path_label(path, parent_expected, parent_actual))
        diffs.append(diff)
    return diffs


def format_diff_line(diff: Dict[str, Any]) -> str:
    name = diff.get("label") or diff.get("path", "")
    if diff.get("path") and diff.get("label") and diff["path"] != diff["label"]:
        name = f"{diff['label']}.{diff['path'].split(']', 1)[-1].lstrip('.')}"
    if diff.get("type") == "value_mismatch":
        return f"{name}: expected={diff.get('expected')!r}, actual={diff.get('actual')!r}"
    if diff.get("type") == "missing":
        return f"{name}: missing, expected={diff.get('expected')!r}"
    if diff.get("type") == "extra":
        return f"{name}: extra, actual={diff.get('actual')!r}"
    return f"{name}: {diff.get('type', 'diff')}"


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def build_common_launch_args(raw_launch_args: List[str]) -> Dict[str, str]:
    launch_args: Dict[str, str] = dict(DEFAULT_STRESS_LAUNCH_ARGS)
    for raw_arg in raw_launch_args:
        key, value = parse_launch_arg(raw_arg)
        launch_args[key] = value
    return launch_args


def build_camera_launch_args(
    *,
    common_launch_args: Dict[str, str],
    camera: CameraSpec,
    config_json: Path,
) -> Dict[str, str]:
    launch_args = dict(common_launch_args)
    launch_args["camera_name"] = camera.name
    launch_args["load_config_json_file_path"] = str(config_json)
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


def build_summary(result: Dict[str, Any]) -> str:
    tests = result.get("tests", [])
    status_counts: Dict[str, int] = {}
    failed_tests = []
    for test in tests:
        status = str(test.get("status", "unknown") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "passed":
            failed_tests.append(test)

    planned_runs = result.get("run_count")
    lines = [
        "# Export Load Stress Test",
        "",
        *test_environment_markdown(result.get("environment", {})),
        "## Result",
        "",
        f"- Status: {result.get('status', '')}",
        f"- Tool version: {result.get('tool_version', '')}",
        f"- Passed tests: {result.get('passed_tests', 0)}",
        f"- Planned runs: {planned_runs if planned_runs is not None else 'duration-limited'}",
        f"- Completed tests: {len(tests)}",
        f"- Failed tests: {len(failed_tests)}",
        f"- Elapsed seconds: {float(result.get('elapsed_seconds', 0.0) or 0.0):.1f}",
        f"- Visual artifacts per topic per test: {result.get('save_image_count', 0)}",
        "",
        "## Cameras",
        "",
    ]
    for camera in result.get("cameras", []):
        lines.append(f"- {camera.get('name', '')}")
    lines.extend(["", "## Config JSON Cycle", ""])
    for path in result.get("config_jsons", []):
        lines.append(f"- {path}")
    lines.extend(["", "## Point Cloud Topics", ""])
    lines.extend(f"- {topic}" for topic in result.get("point_cloud_topics", []))
    if not result.get("point_cloud_topics"):
        lines.append("- None")
    lines.extend(["", "## IMU Topics", ""])
    lines.extend(f"- {topic}" for topic in result.get("imu_topics", []))
    if not result.get("imu_topics"):
        lines.append("- None")
    if result.get("manual_confirmation_message"):
        lines.extend(["", "## Manual Confirmation", "", result["manual_confirmation_message"]])
    if result.get("error"):
        lines.extend(["", "## Error", "", str(result["error"])])

    lines.extend(["", "## Test Statistics", ""])
    if status_counts:
        for status in sorted(status_counts):
            lines.append(f"- {status}: {status_counts[status]}")
    else:
        lines.append("- No tests recorded")

    if not failed_tests:
        lines.extend(["", "## Failures", "", "- None"])
        return "\n".join(lines) + "\n"

    lines.extend(["", "## Failures", ""])
    for test in failed_tests:
        lines.append(
            f"- test_{int(test.get('test_index', 0)):04d}: "
            f"{test.get('status', '')} ({Path(test.get('config_json', '')).name})"
        )
        if test.get("message"):
            lines.append(f"  {test['message']}")
        for export_result in test.get("exports", []):
            if export_result.get("status") == "passed":
                continue
            lines.append(
                f"  - {export_result.get('camera', '')}: "
                f"{export_result.get('message', '')}"
            )
            for diff in export_result.get("diff_preview", []):
                lines.append(f"    - {format_diff_line(diff)}")
            if export_result.get("diff_path"):
                lines.append(f"    - diff: {export_result['diff_path']}")
    return "\n".join(lines) + "\n"


def export_and_compare_camera(
    *,
    harness: RosHarness,
    camera: CameraSpec,
    input_json_path: Path,
    input_payload: Dict[str, Any],
    export_service_template: str,
    export_service_type: str,
    export_path: Path,
    diff_path: Path,
    service_timeout: float,
) -> Dict[str, Any]:
    service_name = expand_camera_template(export_service_template, camera.name)
    response = harness.call_service(
        service_name,
        export_service_type,
        request_data={"data": str(export_path)},
        timeout=service_timeout,
    )
    success = bool(getattr(response, "success", False))
    message = str(getattr(response, "message", ""))
    result: Dict[str, Any] = {
        "camera": camera.name,
        "service": service_name,
        "status": "passed",
        "success": success,
        "message": message,
        "export_path": str(export_path),
        "diff_path": "",
        "diff_count": 0,
    }
    if not success:
        result["status"] = "failed"
        result["message"] = message or "export_config_json returned success=false"
        return result
    if not export_path.is_file():
        result["status"] = "failed"
        result["message"] = f"exported JSON file not found: {export_path}"
        return result

    exported_payload = load_json_file(export_path)
    diffs = compare_json_values(
        input_payload.get("parameters", {}),
        exported_payload.get("parameters", {}),
    )
    if diffs:
        diff_payload = {
            "camera": camera.name,
            "input_json": str(input_json_path),
            "exported_json": str(export_path),
            "diff_count": len(diffs),
            "diffs": diffs,
        }
        write_json(diff_path, diff_payload)
        result["status"] = "failed"
        result["message"] = f"parameters differ, diff_count={len(diffs)}"
        result["diff_path"] = str(diff_path)
        result["diff_count"] = len(diffs)
        result["diff_preview"] = diffs[:10]
    return result


def run(args) -> int:
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    runtime_env = prepare_runtime_env(args)
    apply_python_paths(runtime_env)
    environment = collect_test_environment(args)

    config_jsons = normalize_config_paths(args.config_json)
    cameras = [parse_camera_spec(raw) for raw in (args.camera or ["name=camera"])]
    common_launch_args = build_common_launch_args(args.launch_arg)
    run_count = args.run_count
    duration_text = str(args.duration or "").strip()
    if not duration_text and run_count is None:
        raise ValueError("at least one of --duration or --run-count is required")
    if run_count is not None and run_count <= 0:
        raise ValueError("--run-count must be > 0")
    duration_seconds = (
        parse_duration(duration_text, 0.0) if duration_text else None
    )

    stable_seconds = parse_duration(args.stable_seconds, 5.0)
    stream_timeout = parse_duration(args.stream_timeout, 60.0)
    max_gap_seconds = parse_duration(args.max_gap_seconds, 1.5)
    restart_delay = parse_duration(args.restart_delay, 2.0)
    launch_start_interval = parse_duration(args.launch_start_interval, 2.0)
    service_timeout = parse_duration(args.service_timeout, 30.0)
    save_image_count = int(args.save_image_count)
    if save_image_count < 0:
        raise ValueError("--save-image-count must be >= 0")
    save_image_timeout = parse_duration(args.save_image_timeout, 30.0)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_export_load")
    run_started_at = iso_now()
    default_results_dir = Path(__file__).resolve().parent / "results" / run_id
    results_dir = ensure_dir(Path(args.results_dir).resolve() if args.results_dir else default_results_dir)
    install_terminal_log(results_dir / "terminal.log")
    exports_dir = ensure_dir(results_dir / "exports")
    events = EventWriter(results_dir / "events.jsonl")
    emit = StatusLogger(events)

    image_topic_templates = [topic.strip() for topic in args.image_topic if topic.strip()]
    auto_discover_image_topics = not image_topic_templates
    image_topics = [
        expand_camera_template(topic_template, camera.name)
        for camera in cameras
        for topic_template in image_topic_templates
    ]
    topic_cameras = {
        expand_camera_template(topic_template, camera.name): camera.name
        for camera in cameras
        for topic_template in image_topic_templates
    }
    camera_names = [camera.name for camera in cameras]
    configured_point_cloud_topics = expand_topic_templates(
        args.point_cloud_topic, camera_names
    )
    configured_imu_topics = expand_topic_templates(args.imu_topic, camera_names)
    sensor_baseline: Optional[tuple[List[str], List[str], Dict[str, str]]] = None

    result: Dict[str, Any] = {
        "status": "passed",
        "tool_version": TOOL_VERSION,
        "environment": environment,
        "ros_version": args.ros_version,
        "launch_file": args.launch_file,
        "launch_package": args.launch_package,
        "run_count": run_count,
        "continue_on_failure": args.continue_on_failure,
        "duration_limit_seconds": duration_seconds,
        "passed_tests": 0,
        "config_jsons": [str(path) for path in config_jsons],
        "cameras": [asdict(camera) for camera in cameras],
        "image_topics": image_topics,
        "point_cloud_topics": configured_point_cloud_topics,
        "imu_topics": configured_imu_topics,
        "stable_seconds_required": stable_seconds,
        "stream_timeout_seconds": stream_timeout,
        "max_gap_seconds": max_gap_seconds,
        "restart_delay_seconds": restart_delay,
        "save_image_count": save_image_count,
        "save_image_timeout_seconds": save_image_timeout,
        "tests": [],
        "elapsed_seconds": 0.0,
    }

    emit(f"tool version: {TOOL_VERSION}")
    emit(f"results dir: {results_dir}")
    emit("test started", event="phase", phase="starting")
    emit(f"run count: {run_count if run_count is not None else 'duration-limited'}")
    emit(f"cameras: {', '.join(camera.name for camera in cameras)}")
    emit(f"config JSON cycle: {', '.join(str(path) for path in config_jsons)}")
    if auto_discover_image_topics:
        emit("monitor topics: auto-discover all published image streams")
    else:
        emit(f"monitor topics: {', '.join(image_topics)}")
    if save_image_count > 0:
        emit(f"save images and sensor plots: {save_image_count} per topic")
    else:
        emit("save images: disabled")

    test_start_monotonic = time.monotonic()
    deadline = (
        test_start_monotonic + duration_seconds
        if duration_seconds is not None
        else None
    )
    active_sessions: List[LaunchSession] = []
    current_test: Optional[Dict[str, Any]] = None
    keep_launch_running = False
    try:
        with RosHarness(args.ros_version, "export_load_stress_test", args.queue_size) as harness:
            test_index = 0
            while run_count is None or test_index < run_count:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                test_index += 1
                config_json = config_jsons[(test_index - 1) % len(config_jsons)]
                input_payload = load_json_file(config_json)
                test_name = f"test_{test_index:04d}"
                test_export_dir = ensure_dir(exports_dir / test_name)
                test_payload: Dict[str, Any] = {
                    "test_index": test_index,
                    "status": "running",
                    "message": "",
                    "config_json": str(config_json),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "ended_at": "",
                    "launches": [],
                    "topics": [],
                    "images": [],
                    "sensors": [],
                    "exports": [],
                }
                current_test = test_payload
                result["tests"].append(test_payload)

                emit(
                    f"{test_name}: start, config={config_json.name}",
                    event="progress",
                    current=test_index,
                    total=run_count,
                    phase="running",
                )
                sessions: List[LaunchSession] = []
                for camera in cameras:
                    launch_args = build_camera_launch_args(
                        common_launch_args=common_launch_args,
                        camera=camera,
                        config_json=config_json,
                    )
                    launch_args["log_level"] = args.sdk_log_level
                    launch_args["log_file_name"] = f"{camera.name}.log"
                    command = build_launch_command(
                        ros_version=args.ros_version,
                        launch_package=args.launch_package,
                        launch_file=args.launch_file,
                        launch_args=launch_args,
                    )
                    safe_camera_name = sanitize_path_part(camera.name)
                    camera_log_dir = ensure_dir(results_dir / "logs" / test_name / safe_camera_name)
                    launch_log_path = camera_log_dir / f"{safe_camera_name}.launch.log"
                    session_env = dict(runtime_env)
                    session_env["ORBBEC_LOG_DIR"] = str(
                        ensure_dir(camera_log_dir / "sdk")
                    )
                    session = LaunchSession(
                        camera_name=camera.name,
                        command=command,
                        work_dir=results_dir,
                        env=session_env,
                        log_path=launch_log_path,
                        emit=emit,
                    )
                    sessions.append(session)
                    test_payload["launches"].append(
                        {
                            "camera": camera.name,
                            "command": command,
                            "launch_args": launch_args,
                            "launch_log": str(launch_log_path),
                        }
                    )
                active_sessions = sessions

                for index, session in enumerate(sessions):
                    emit(f"{test_name}: start launch for {session.camera_name}")
                    session.start()
                    if index < len(sessions) - 1:
                        time.sleep(launch_start_interval)

                if auto_discover_image_topics:
                    image_topics, topic_cameras = discover_image_topics(
                        harness=harness,
                        camera_names=camera_names,
                        sessions=sessions,
                        timeout=stream_timeout,
                    )
                    result["image_topics"] = sorted(
                        set(result["image_topics"]) | set(image_topics)
                    )
                    emit(f"{test_name}: discovered image topics: {', '.join(image_topics)}")

                ok, snapshot, message = wait_for_stable_streams(
                    sessions=sessions,
                    harness=harness,
                    topics=image_topics,
                    stable_seconds=stable_seconds,
                    timeout=stream_timeout,
                    max_gap_seconds=max_gap_seconds,
                    emit=emit,
                )
                test_payload["topics"] = snapshot
                if not ok:
                    test_payload["status"] = "failed"
                    test_payload["message"] = message
                    test_payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    result["status"] = "failed"
                    result.setdefault("errors", []).append(f"{test_name}: {message}")
                    if args.continue_on_failure:
                        emit(f"{test_name}: streams not stable; continuing after cleanup")
                        for session in reversed(sessions):
                            session.stop()
                        active_sessions = []
                        current_test = None
                        continue
                    result["manual_confirmation_required"] = True
                    result["manual_confirmation_message"] = (
                        f"{test_name}: {message}; launches were kept running until manual "
                        "check finished"
                    )
                    keep_launch_running = True
                    emit(f"{test_name}: streams not stable, launches kept running")
                    emit("please manually check the launches, press Ctrl+C to stop them and finish")
                    while any(session.poll() is None for session in sessions):
                        time.sleep(1.0)
                    break

                ok, image_snapshot, image_message = save_images(
                    sessions=sessions,
                    harness=harness,
                    topics=image_topics,
                    topic_cameras=topic_cameras,
                    output_root=results_dir / "images",
                    count_per_topic=save_image_count,
                    timeout=save_image_timeout,
                    emit=emit,
                )
                test_payload["images"] = image_snapshot
                if not ok:
                    test_payload["status"] = "failed"
                    test_payload["message"] = image_message
                    test_payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    result["status"] = "failed"
                    result.setdefault("errors", []).append(
                        f"{test_name}: {image_message}"
                    )
                    if args.continue_on_failure:
                        emit(f"{test_name}: image save failed; continuing after cleanup")
                        for session in reversed(sessions):
                            session.stop()
                        active_sessions = []
                        current_test = None
                        continue
                    result["manual_confirmation_required"] = True
                    result["manual_confirmation_message"] = (
                        f"{test_name}: {image_message}; launches were kept running until manual "
                        "check finished"
                    )
                    keep_launch_running = True
                    emit(f"{test_name}: image save failed")
                    emit("please manually check the launches, press Ctrl+C to stop them and finish")
                    while any(session.poll() is None for session in sessions):
                        time.sleep(1.0)
                    break
                emit(f"{test_name}: {image_message}")

                if sensor_baseline is None:
                    sensor_baseline = discover_sensor_topics(
                        harness=harness,
                        camera_names=camera_names,
                        point_cloud_topics=configured_point_cloud_topics,
                        imu_topics=configured_imu_topics,
                        timeout=stream_timeout,
                        ensure_running=lambda: [session.assert_running() for session in sessions],
                    )
                    result["point_cloud_topics"] = sensor_baseline[0]
                    result["imu_topics"] = sensor_baseline[1]
                    emit(
                        f"sensor baseline: {len(sensor_baseline[0])} point cloud, "
                        f"{len(sensor_baseline[1])} IMU topic(s)"
                    )
                point_cloud_topics, imu_topics, sensor_topic_cameras = sensor_baseline
                sensor_timeout = max(
                    save_image_timeout,
                    2.0 * max(save_image_count, 1) + 5.0,
                )
                ok, sensor_snapshot, sensor_message = capture_sensor_artifacts(
                    harness=harness,
                    point_cloud_topics=point_cloud_topics,
                    imu_topics=imu_topics,
                    topic_cameras=sensor_topic_cameras,
                    output_root=results_dir / "images",
                    save_count=save_image_count,
                    timeout=sensor_timeout,
                    ensure_running=lambda: [session.assert_running() for session in sessions],
                )
                test_payload["sensors"] = sensor_snapshot
                if not ok:
                    test_payload["status"] = "failed"
                    test_payload["message"] = sensor_message
                    test_payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    result["status"] = "failed"
                    result.setdefault("errors", []).append(
                        f"{test_name}: {sensor_message}"
                    )
                    if args.continue_on_failure:
                        emit(
                            f"{test_name}: sensor artifact capture failed; "
                            "continuing after cleanup"
                        )
                        for session in reversed(sessions):
                            session.stop()
                        active_sessions = []
                        current_test = None
                        continue
                    raise RuntimeError(sensor_message)
                emit(f"{test_name}: {sensor_message}")

                emit(f"{test_name}: streams stable, export and compare JSON")
                camera_results = []
                for camera in cameras:
                    camera_export_dir = ensure_dir(test_export_dir / camera.name)
                    export_path = camera_export_dir / f"{config_json.stem}_exported.json"
                    diff_path = camera_export_dir / "diff_parameters.json"
                    camera_result = export_and_compare_camera(
                        harness=harness,
                        camera=camera,
                        input_json_path=config_json,
                        input_payload=input_payload,
                        export_service_template=args.export_service,
                        export_service_type=args.export_service_type,
                        export_path=export_path,
                        diff_path=diff_path,
                        service_timeout=service_timeout,
                    )
                    camera_results.append(camera_result)
                    test_payload["exports"].append(camera_result)

                failed_camera_results = [
                    item for item in camera_results if item.get("status") != "passed"
                ]
                if failed_camera_results:
                    failed_names = ", ".join(item["camera"] for item in failed_camera_results)
                    test_payload["status"] = "failed"
                    test_payload["message"] = f"export compare failed for: {failed_names}"
                    test_payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    result["status"] = "failed"
                    result.setdefault("errors", []).append(
                        f"{test_name}: export compare failed for {failed_names}"
                    )
                    if args.continue_on_failure:
                        emit(
                            f"{test_name}: export compare failed for {failed_names}; "
                            "continuing after cleanup"
                        )
                        for session in reversed(sessions):
                            session.stop()
                        active_sessions = []
                        current_test = None
                        continue
                    result["manual_confirmation_required"] = True
                    result["manual_confirmation_message"] = (
                        f"{test_name}: export compare failed for {failed_names}; "
                        "launches were kept running until manual check finished"
                    )
                    keep_launch_running = True
                    emit(f"{test_name}: export compare failed for {failed_names}")
                    emit("please manually check the launches, press Ctrl+C to stop them and finish")
                    while any(session.poll() is None for session in sessions):
                        time.sleep(1.0)
                    break

                emit(f"{test_name}: passed, stop launches")
                for session in reversed(sessions):
                    session.stop()
                active_sessions = []
                test_payload["status"] = "passed"
                test_payload["message"] = "all cameras exported matching parameters"
                test_payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
                result["passed_tests"] += 1
                current_test = None
                if (run_count is None or test_index < run_count) and (
                    deadline is None or time.monotonic() < deadline
                ):
                    time.sleep(restart_delay)
    except KeyboardInterrupt:
        if result.get("manual_confirmation_required"):
            emit("manual check finished by user")
            keep_launch_running = False
        else:
            result["status"] = "interrupted"
            emit("test interrupted by user")
        if current_test is not None and not result.get("manual_confirmation_required"):
            current_test["status"] = "interrupted"
            current_test["message"] = "interrupted by user"
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            result["status"] = "interrupted"
            emit("test interrupted by user")
            if current_test is not None:
                current_test["status"] = "interrupted"
                current_test["message"] = "interrupted by user"
        else:
            result["status"] = "failed"
            result["error"] = str(exc)
            emit(f"test failed: {exc}")
            if current_test is not None:
                current_test["status"] = "failed"
                current_test["message"] = str(exc)
    finally:
        if active_sessions and not keep_launch_running:
            emit("stop active launches")
            for session in reversed(active_sessions):
                session.stop()
        result["elapsed_seconds"] = time.monotonic() - test_start_monotonic
        for test_payload in result.get("tests", []):
            if not test_payload.get("ended_at"):
                test_payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
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
                "passed_runs": result["passed_tests"],
                "completed_runs": len(result["tests"]),
            },
            artifacts=artifact_list(results_dir),
        )
        atomic_write_json(results_dir / "result.json", payload)
        signal.signal(signal.SIGINT, previous_sigint_handler)

    if result["status"] == "passed":
        emit(f"test finished successfully, passed_tests={result['passed_tests']}")
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Repeatedly load config JSON files through single-camera launches, "
            "export current JSON settings, and compare parameters."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 ./export_load_stress_test/export_load_stress_test.py "
            "--run-count 2 --camera name=camera\n\n"
            "  python3 ./export_load_stress_test/export_load_stress_test.py --run-count 2 "
            "--camera name=camera_01,usb-port=2-1 "
            "--camera name=camera_02,usb-port=2-3\n"
        ),
    )
    parser.add_argument("--ros-version", choices=("1", "2"), default=os.environ.get("ROS_VERSION", "2"))
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument("--driver-setup", default=os.environ.get("ORBBEC_CAMERA_SETUP", ""))
    parser.add_argument("--launch-package", default="orbbec_camera")
    parser.add_argument("--launch-file", default="gemini_330_series_sdk_json.launch.py")
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra launch arg, KEY=VALUE or KEY:=VALUE")
    parser.add_argument(
        "--sdk-log-level",
        choices=("debug", "info", "warn", "error", "fatal", "none"),
        default="debug",
        help="Orbbec SDK log level (default: debug)",
    )
    parser.add_argument("--config-json", action="append", default=[], help="Config JSON file, repeat at least twice")
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
        "--run-count", type=int, default=None, help="Optional maximum complete test cycles"
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue with the next test cycle after a failed cycle (default: stop)",
    )
    parser.add_argument(
        "--duration",
        default="",
        help="Optional maximum wall time, such as 300, 15m, or 2h",
    )
    parser.add_argument(
        "--image-topic",
        action="append",
        default=[],
        help=(
            "Image or CompressedImage topic template to monitor and save, supports "
            "{camera}; can repeat. "
            "When omitted, all published image streams under each camera are discovered."
        ),
    )
    parser.add_argument(
        "--point-cloud-topic",
        action="append",
        default=[],
        help=(
            "PointCloud2 topic template to require, supports {camera}; can repeat. "
            "When omitted, published point cloud topics are discovered on the first test."
        ),
    )
    parser.add_argument(
        "--imu-topic",
        action="append",
        default=[],
        help=(
            "Imu topic template to require, supports {camera}; can repeat. "
            "When omitted, published IMU topics are discovered on the first test."
        ),
    )
    parser.add_argument("--stable-seconds", default="5")
    parser.add_argument("--stream-timeout", default="60")
    parser.add_argument("--max-gap-seconds", default="1.5")
    parser.add_argument("--restart-delay", default="2")
    parser.add_argument(
        "--launch-start-interval",
        default="2",
        help="Delay in seconds between starting each camera launch (default: 2)",
    )
    parser.add_argument("--queue-size", type=int, default=10)
    parser.add_argument(
        "--save-image-count",
        type=int,
        default=1,
        help=(
            "PNG artifacts to save per image, point cloud, and IMU topic for each "
            "test; use 0 to disable saving while keeping stream validation"
        ),
    )
    parser.add_argument("--save-image-timeout", default="30", help="Max wait time for image saving")
    parser.add_argument("--export-service", default="/{camera}/export_config_json")
    parser.add_argument("--export-service-type", default="orbbec_camera_msgs/srv/SetString")
    parser.add_argument("--service-timeout", default="30")
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
