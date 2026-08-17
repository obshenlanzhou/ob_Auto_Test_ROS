#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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


ENV_READY_VAR = "STREAM_TOGGLE_STRESS_TEST_ENV_READY"
INTERRUPTED = False
TOOL_VERSION = "1.9"
TEST_ID = "stream_toggle_stress_test"
DEFAULT_STRESS_LAUNCH_ARGS = {
    "enable_heartbeat": "true",
    "enable_firmware_log": "true",
}
RAW_IMAGE_TYPES = {"sensor_msgs/msg/Image", "sensor_msgs/Image"}
COMPRESSED_IMAGE_TYPES = {
    "sensor_msgs/msg/CompressedImage",
    "sensor_msgs/CompressedImage",
}
SET_BOOL_TYPES = {"std_srvs/srv/SetBool", "std_srvs/SetBool"}
SET_STREAM_PROFILE_TYPES = {
    "orbbec_camera_msgs/srv/SetStreamProfile",
    "orbbec_camera/SetStreamProfile",
}


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handle_sigint(signum, frame) -> None:
    del signum, frame
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


def parse_launch_arg(raw: str) -> Tuple[str, str]:
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
    launch_args: Dict[str, str], raw_launch_args: Sequence[str]
) -> Dict[str, str]:
    merged = dict(launch_args)
    for raw_arg in raw_launch_args:
        key, value = parse_launch_arg(raw_arg)
        merged[key] = value
    return merged


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_topic(topic: str) -> str:
    normalized = "/" + str(topic or "").strip().strip("/")
    return normalized if normalized != "/" else ""


def expand_camera_topic(topic: str, camera_name: str) -> str:
    return topic.replace("{camera}", camera_name).replace("${camera}", camera_name)


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
        ensure_dir(self.log_path.parent)
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
        return None if self.process is None else self.process.poll()

    def assert_running(self) -> None:
        code = self.poll()
        if code is not None:
            raise RuntimeError(f"launch process exited unexpectedly with code {code}")

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


@dataclass(frozen=True)
class StreamTarget:
    topic: str
    camera_namespace: str
    camera_name: str
    stream: str
    service: str


@dataclass(frozen=True)
class SaveImageTarget:
    topic: str
    target_topic: str
    topic_kind: str
    camera_name: str
    stream: str


@dataclass(frozen=True)
class StreamGroupTarget:
    camera_namespace: str
    camera_name: str
    service: str
    topics: Tuple[str, ...]

    @property
    def topic(self) -> str:
        return f"{self.camera_namespace}/*"


@dataclass(frozen=True)
class StreamProfileSpec:
    topic: str
    camera_namespace: str
    camera_name: str
    stream: str
    width: int
    height: int
    fps: int
    format: str


@dataclass(frozen=True)
class StreamProfileGroup:
    camera_namespace: str
    camera_name: str
    service: str
    profiles: Tuple[StreamProfileSpec, ...]


def stream_target_from_topic(topic: str) -> StreamTarget:
    normalized = normalize_topic(topic)
    parts = [item for item in normalized.split("/") if item]
    if len(parts) < 3 or parts[-1] != "image_raw":
        raise ValueError(
            f"image topic must match /<camera-namespace>/<stream>/image_raw: {topic}"
        )
    stream = parts[-2]
    namespace_parts = parts[:-2]
    if not namespace_parts:
        raise ValueError(f"image topic does not contain a camera namespace: {topic}")
    camera_namespace = "/" + "/".join(namespace_parts)
    camera_name = "_".join(namespace_parts)
    service = f"{camera_namespace}/toggle_{stream}"
    return StreamTarget(normalized, camera_namespace, camera_name, stream, service)


def save_image_target_from_topic(topic: str) -> SaveImageTarget:
    normalized = normalize_topic(topic)
    compressed_suffix = "/compressed"
    if normalized.endswith(compressed_suffix):
        target_topic = normalized[: -len(compressed_suffix)]
        topic_kind = "compressed"
    else:
        target_topic = normalized
        topic_kind = "raw"
    stream_target = stream_target_from_topic(target_topic)
    return SaveImageTarget(
        topic=normalized,
        target_topic=stream_target.topic,
        topic_kind=topic_kind,
        camera_name=stream_target.camera_name,
        stream=stream_target.stream,
    )


def build_save_image_targets(
    targets: Sequence[StreamTarget],
    requested_topics: Sequence[str],
    topic_types: Dict[str, List[str]],
) -> Dict[str, List[SaveImageTarget]]:
    selected_topics = {target.topic for target in targets}
    topics = list(requested_topics) if requested_topics else sorted(selected_topics)
    by_target: Dict[str, List[SaveImageTarget]] = {
        target.topic: [] for target in targets
    }
    errors: List[str] = []
    for topic in topics:
        try:
            save_target = save_image_target_from_topic(topic)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if save_target.target_topic not in selected_topics:
            errors.append(
                f"save image topic does not match a selected stream: {save_target.topic}"
            )
            continue
        advertised_types = topic_types.get(save_target.topic, [])
        expected_types = (
            COMPRESSED_IMAGE_TYPES
            if save_target.topic_kind == "compressed"
            else RAW_IMAGE_TYPES
        )
        if not advertised_types:
            errors.append(f"save image topic not advertised: {save_target.topic}")
            continue
        if not expected_types.intersection(advertised_types):
            errors.append(
                f"save image topic has incompatible type: {save_target.topic} "
                f"({', '.join(advertised_types)})"
            )
            continue
        by_target[save_target.target_topic].append(save_target)
    if errors:
        raise RuntimeError("save image topic preflight failed: " + "; ".join(errors))
    return by_target


def parse_stream_profile_spec(raw: str, camera_name: str = "") -> StreamProfileSpec:
    text = expand_camera_topic(str(raw or "").strip(), camera_name)
    if "=" not in text:
        raise ValueError(
            "stream profile must be TOPIC=WIDTHxHEIGHT@FPS[:FORMAT]: " + str(raw)
        )
    topic_text, profile_text = text.rsplit("=", 1)
    target = stream_target_from_topic(topic_text.strip())
    match = re.fullmatch(
        r"([1-9]\d*)[xX]([1-9]\d*)@([1-9]\d*)(?::([A-Za-z0-9_]+))?",
        profile_text.strip(),
    )
    if not match:
        raise ValueError(
            "stream profile must be TOPIC=WIDTHxHEIGHT@FPS[:FORMAT] with positive "
            "integers and a valid format token: "
            + str(raw)
        )
    width, height, fps = (int(item) for item in match.groups()[:3])
    stream_format = str(match.group(4) or "").upper()
    return StreamProfileSpec(
        topic=target.topic,
        camera_namespace=target.camera_namespace,
        camera_name=target.camera_name,
        stream=target.stream,
        width=width,
        height=height,
        fps=fps,
        format=stream_format,
    )


def is_raw_image_type(type_names: Sequence[str]) -> bool:
    return any(type_name in RAW_IMAGE_TYPES for type_name in type_names)


def is_set_bool_type(type_names: Sequence[str]) -> bool:
    return any(type_name in SET_BOOL_TYPES for type_name in type_names)


def is_set_stream_profile_type(type_names: Sequence[str]) -> bool:
    return any(type_name in SET_STREAM_PROFILE_TYPES for type_name in type_names)


def build_profile_groups(
    specs: Sequence[StreamProfileSpec],
    targets: Sequence[StreamTarget],
    service_types: Dict[str, List[str]],
) -> List[StreamProfileGroup]:
    target_topics = {target.topic for target in targets}
    profiles_by_namespace: Dict[str, List[StreamProfileSpec]] = {}
    camera_names: Dict[str, str] = {}
    errors = []
    for spec in specs:
        if spec.topic not in target_topics:
            errors.append(f"profile topic is not a selected target stream: {spec.topic}")
            continue
        profiles_by_namespace.setdefault(spec.camera_namespace, []).append(spec)
        camera_names[spec.camera_namespace] = spec.camera_name
    groups = []
    for namespace in sorted(profiles_by_namespace):
        service = f"{namespace}/set_stream_profile"
        advertised_types = service_types.get(service, [])
        if not advertised_types:
            errors.append(f"stream-profile service not advertised: {service}")
            continue
        if not is_set_stream_profile_type(advertised_types):
            errors.append(
                f"stream-profile service has incompatible type: {service} "
                f"({', '.join(advertised_types)})"
            )
            continue
        groups.append(
            StreamProfileGroup(
                camera_namespace=namespace,
                camera_name=camera_names[namespace],
                service=service,
                profiles=tuple(
                    sorted(profiles_by_namespace[namespace], key=lambda item: item.stream)
                ),
            )
        )
    if errors:
        raise RuntimeError("stream-profile preflight failed: " + "; ".join(errors))
    return groups


def build_stream_groups(
    targets: Sequence[StreamTarget], service_types: Dict[str, List[str]]
) -> List[StreamGroupTarget]:
    topics_by_namespace: Dict[str, List[str]] = {}
    camera_names: Dict[str, str] = {}
    for target in targets:
        topics_by_namespace.setdefault(target.camera_namespace, []).append(target.topic)
        camera_names[target.camera_namespace] = target.camera_name
    groups = []
    errors = []
    for namespace in sorted(topics_by_namespace):
        service = f"{namespace}/set_streams_enable"
        advertised_types = service_types.get(service, [])
        if not advertised_types:
            errors.append(f"all-stream service not advertised: {service}")
            continue
        if not is_set_bool_type(advertised_types):
            errors.append(
                f"all-stream service is not std_srvs/SetBool: {service} "
                f"({', '.join(advertised_types)})"
            )
            continue
        groups.append(
            StreamGroupTarget(
                camera_namespace=namespace,
                camera_name=camera_names[namespace],
                service=service,
                topics=tuple(sorted(topics_by_namespace[namespace])),
            )
        )
    if errors:
        raise RuntimeError("all-stream preflight failed: " + "; ".join(errors))
    return groups


class RosHarness:
    def __init__(
        self,
        ros_version: str,
        node_name: str,
        queue_size: int,
        enable_profile_switch: bool = False,
    ) -> None:
        self.ros_version = ros_version
        self.node_name = node_name
        self.queue_size = queue_size
        self.enable_profile_switch = enable_profile_switch
        self._rclpy = None
        self._rospy = None
        self._image_type = None
        self._compressed_image_type = None
        self._point_cloud_type = None
        self._imu_type = None
        self._set_bool_type = None
        self._set_stream_profile_type = None
        self._set_stream_profile_request_type = None
        self._stream_profile_message_type = None
        self._rosservice = None
        self._sensor_qos = None
        self.node = None
        self.subscriptions = []

    def __enter__(self) -> "RosHarness":
        if self.ros_version == "2":
            try:
                import rclpy
                from rclpy.qos import qos_profile_sensor_data
                from sensor_msgs.msg import CompressedImage, Image, Imu, PointCloud2
                from std_srvs.srv import SetBool
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS2 Python modules. Source ROS2 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rclpy.init(args=None)
            self._rclpy = rclpy
            self.node = rclpy.create_node(self.node_name)
            self._image_type = Image
            self._compressed_image_type = CompressedImage
            self._point_cloud_type = PointCloud2
            self._imu_type = Imu
            self._set_bool_type = SetBool
            self._sensor_qos = qos_profile_sensor_data
            if self.enable_profile_switch:
                try:
                    from orbbec_camera_msgs.msg import StreamProfile
                    from orbbec_camera_msgs.srv import SetStreamProfile
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        "failed to import ROS2 set_stream_profile interfaces from "
                        f"orbbec_camera_msgs: {exc}"
                    ) from exc
                self._set_stream_profile_type = SetStreamProfile
                self._set_stream_profile_request_type = SetStreamProfile.Request
                self._stream_profile_message_type = StreamProfile
        else:
            try:
                import rosservice
                import rospy
                from sensor_msgs.msg import CompressedImage, Image, Imu, PointCloud2
                from std_srvs.srv import SetBool
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "failed to import ROS1 Python modules. Source ROS1 and camera setup "
                    "before running, or pass --ros-setup/--driver-setup. "
                    f"Original error: {exc}"
                ) from exc
            rospy.init_node(self.node_name, anonymous=True, disable_signals=True)
            self._rospy = rospy
            self._rosservice = rosservice
            self._image_type = Image
            self._compressed_image_type = CompressedImage
            self._point_cloud_type = PointCloud2
            self._imu_type = Imu
            self._set_bool_type = SetBool
            if self.enable_profile_switch:
                try:
                    from orbbec_camera.msg import StreamProfile
                    from orbbec_camera.srv import SetStreamProfile, SetStreamProfileRequest
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        "failed to import ROS1 set_stream_profile interfaces from "
                        f"orbbec_camera: {exc}"
                    ) from exc
                self._set_stream_profile_type = SetStreamProfile
                self._set_stream_profile_request_type = SetStreamProfileRequest
                self._stream_profile_message_type = StreamProfile
        return self

    def get_topic_names_and_types(self) -> Dict[str, List[str]]:
        if self.ros_version == "2":
            return {
                normalize_topic(name): list(types)
                for name, types in self.node.get_topic_names_and_types()
            }
        return {
            normalize_topic(name): [type_name]
            for name, type_name in self._rospy.get_published_topics(namespace="/")
        }

    def get_service_names_and_types(self) -> Dict[str, List[str]]:
        if self.ros_version == "2":
            return {
                normalize_topic(name): list(types)
                for name, types in self.node.get_service_names_and_types()
            }
        services: Dict[str, List[str]] = {}
        for name in self._rosservice.get_service_list():
            normalized = normalize_topic(name)
            try:
                type_name = self._rosservice.get_service_type(name)
            except Exception:  # service may disappear while the graph is queried
                type_name = None
            services[normalized] = [type_name] if type_name else []
        return services

    def create_image_subscription(self, topic: str, callback, topic_kind: str = "raw"):
        message_type = (
            self._compressed_image_type if topic_kind == "compressed" else self._image_type
        )
        if self.ros_version == "2":
            subscription = self.node.create_subscription(
                message_type, topic, callback, self._sensor_qos
            )
        else:
            subscription = self._rospy.Subscriber(
                topic, message_type, callback, queue_size=self.queue_size
            )
        self.subscriptions.append(subscription)
        return subscription

    def create_sensor_subscription(self, topic: str, kind: str, callback):
        message_type = self._point_cloud_type if kind == "point_cloud" else self._imu_type
        if self.ros_version == "2":
            subscription = self.node.create_subscription(
                message_type, topic, callback, self._sensor_qos
            )
        else:
            subscription = self._rospy.Subscriber(
                topic, message_type, callback, queue_size=self.queue_size
            )
        self.subscriptions.append(subscription)
        return subscription

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

    def call_set_bool(self, service_name: str, enabled: bool, timeout: float) -> Dict[str, Any]:
        if self.ros_version == "2":
            client = self.node.create_client(self._set_bool_type, service_name)
            try:
                started = time.monotonic()
                if not client.wait_for_service(timeout_sec=timeout):
                    raise TimeoutError(f"service not available within {timeout:.1f}s")
                remaining = max(timeout - (time.monotonic() - started), 0.001)
                request = self._set_bool_type.Request()
                request.data = enabled
                future = client.call_async(request)
                deadline = time.monotonic() + remaining
                while not future.done() and time.monotonic() < deadline:
                    self.spin_once(min(0.1, max(deadline - time.monotonic(), 0.001)))
                if not future.done():
                    raise TimeoutError(f"service call timed out after {timeout:.1f}s")
                response = future.result()
                if response is None:
                    raise RuntimeError("service returned no response")
                return {
                    "success": bool(response.success),
                    "message": str(response.message or ""),
                }
            finally:
                self.node.destroy_client(client)

        self._rospy.wait_for_service(service_name, timeout=timeout)
        holder: Dict[str, Any] = {}

        def invoke() -> None:
            try:
                proxy = self._rospy.ServiceProxy(service_name, self._set_bool_type)
                holder["response"] = proxy(enabled)
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"service call timed out after {timeout:.1f}s")
        if "error" in holder:
            raise RuntimeError(str(holder["error"]))
        response = holder["response"]
        return {
            "success": bool(response.success),
            "message": str(response.message or ""),
        }

    def call_set_stream_profile(
        self,
        service_name: str,
        profiles: Sequence[StreamProfileSpec],
        timeout: float,
    ) -> Dict[str, Any]:
        if not self.enable_profile_switch or self._set_stream_profile_type is None:
            raise RuntimeError("stream-profile support was not initialized")
        request = self._set_stream_profile_request_type()
        for profile in profiles:
            message = self._stream_profile_message_type()
            message.stream_name = profile.stream
            message.width = profile.width
            message.height = profile.height
            message.fps = profile.fps
            message.format = profile.format
            request.profiles.append(message)

        if self.ros_version == "2":
            client = self.node.create_client(self._set_stream_profile_type, service_name)
            try:
                started = time.monotonic()
                if not client.wait_for_service(timeout_sec=timeout):
                    raise TimeoutError(f"service not available within {timeout:.1f}s")
                remaining = max(timeout - (time.monotonic() - started), 0.001)
                future = client.call_async(request)
                deadline = time.monotonic() + remaining
                while not future.done() and time.monotonic() < deadline:
                    self.spin_once(min(0.1, max(deadline - time.monotonic(), 0.001)))
                if not future.done():
                    raise TimeoutError(f"service call timed out after {timeout:.1f}s")
                response = future.result()
                if response is None:
                    raise RuntimeError("service returned no response")
                return {
                    "success": bool(response.success),
                    "message": str(response.message or ""),
                }
            finally:
                self.node.destroy_client(client)

        self._rospy.wait_for_service(service_name, timeout=timeout)
        holder: Dict[str, Any] = {}

        def invoke() -> None:
            try:
                proxy = self._rospy.ServiceProxy(service_name, self._set_stream_profile_type)
                holder["response"] = proxy(request)
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"service call timed out after {timeout:.1f}s")
        if "error" in holder:
            raise RuntimeError(str(holder["error"]))
        response = holder["response"]
        return {
            "success": bool(response.success),
            "message": str(response.message or ""),
        }

    def __exit__(self, exc_type, exc, tb) -> None:
        for subscription in list(self.subscriptions):
            try:
                self.destroy_subscription(subscription)
            except Exception:
                pass
        if self.ros_version == "2":
            try:
                self.node.destroy_node()
            except Exception:
                pass
            try:
                if self._rclpy.ok():
                    self._rclpy.shutdown()
            except Exception:
                pass


def _target_sort_key(target: StreamTarget) -> Tuple[str, str, str]:
    return target.camera_namespace, target.stream, target.topic


def evaluate_discovery(
    topic_types: Dict[str, List[str]],
    service_types: Dict[str, List[str]],
) -> Tuple[List[StreamTarget], List[Dict[str, str]]]:
    targets: List[StreamTarget] = []
    skipped: List[Dict[str, str]] = []
    for topic, types in sorted(topic_types.items()):
        if not is_raw_image_type(types):
            continue
        try:
            target = stream_target_from_topic(topic)
        except ValueError as exc:
            skipped.append({"topic": topic, "reason": str(exc)})
            continue
        advertised_types = service_types.get(target.service, [])
        if not advertised_types:
            skipped.append(
                {"topic": topic, "reason": f"toggle service not advertised: {target.service}"}
            )
            continue
        if not is_set_bool_type(advertised_types):
            skipped.append(
                {
                    "topic": topic,
                    "reason": (
                        f"toggle service has incompatible type: {target.service} "
                        f"({', '.join(advertised_types)})"
                    ),
                }
            )
            continue
        targets.append(target)
    return sorted(set(targets), key=_target_sort_key), skipped


def evaluate_explicit_targets(
    explicit_topics: Sequence[str],
    topic_types: Dict[str, List[str]],
    service_types: Dict[str, List[str]],
) -> Tuple[List[StreamTarget], List[str]]:
    targets: List[StreamTarget] = []
    errors: List[str] = []
    for topic in explicit_topics:
        try:
            target = stream_target_from_topic(topic)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        types = topic_types.get(target.topic, [])
        if not types:
            errors.append(f"image topic not advertised: {target.topic}")
            continue
        if not is_raw_image_type(types):
            errors.append(
                f"image topic is not sensor_msgs/Image: {target.topic} ({', '.join(types)})"
            )
            continue
        service_types_for_target = service_types.get(target.service, [])
        if not service_types_for_target:
            errors.append(f"toggle service not advertised: {target.service}")
            continue
        if not is_set_bool_type(service_types_for_target):
            errors.append(
                f"toggle service is not std_srvs/SetBool: {target.service} "
                f"({', '.join(service_types_for_target)})"
            )
            continue
        targets.append(target)
    return sorted(set(targets), key=_target_sort_key), errors


def discover_stream_targets(
    *,
    session: LaunchSession,
    harness: RosHarness,
    explicit_topics: Sequence[str],
    timeout: float,
    settle_seconds: float,
) -> Tuple[List[StreamTarget], List[Dict[str, str]]]:
    deadline = time.monotonic() + timeout
    last_signature: Tuple[Tuple[str, str], ...] = ()
    settled_since: Optional[float] = None
    last_targets: List[StreamTarget] = []
    last_skipped: List[Dict[str, str]] = []
    last_explicit_errors: List[str] = []
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.1)
        topic_types = harness.get_topic_names_and_types()
        service_types = harness.get_service_names_and_types()
        if explicit_topics:
            targets, errors = evaluate_explicit_targets(
                explicit_topics, topic_types, service_types
            )
            last_explicit_errors = errors
            if not errors and len(targets) == len(set(explicit_topics)):
                return targets, []
            continue
        targets, skipped = evaluate_discovery(topic_types, service_types)
        last_targets, last_skipped = targets, skipped
        signature = tuple((target.topic, target.service) for target in targets)
        if signature and signature != last_signature:
            last_signature = signature
            settled_since = time.monotonic()
        elif signature and settled_since is not None:
            if time.monotonic() - settled_since >= settle_seconds:
                return targets, skipped
    if explicit_topics:
        detail = "; ".join(last_explicit_errors) or "targets did not become ready"
        raise RuntimeError(f"explicit stream preflight failed within {timeout:.1f}s: {detail}")
    if last_targets:
        return last_targets, last_skipped
    raise RuntimeError(
        f"no toggle-capable sensor_msgs/Image streams discovered within {timeout:.1f}s"
    )


def _valid_image(message: Any) -> bool:
    return (
        int(getattr(message, "width", 0) or 0) > 0
        and int(getattr(message, "height", 0) or 0) > 0
        and len(getattr(message, "data", b"") or b"") > 0
    )


class StreamMonitor:
    def __init__(self, harness: RosHarness, topics: Sequence[str]) -> None:
        self.harness = harness
        self.topics = list(topics)
        self.lock = threading.Lock()
        now = time.monotonic()
        self.state: Dict[str, Dict[str, Any]] = {
            topic: {
                "message_count": 0,
                "last_message_at": None,
                "latest_message": None,
                "width": 0,
                "height": 0,
                "encoding": "",
                "data_size": 0,
                "window_started_at": now,
                "window_first_message_at": None,
                "window_last_message_at": None,
                "window_max_gap_seconds": 0.0,
                "window_message_count": 0,
            }
            for topic in self.topics
        }
        self.subscriptions = [
            harness.create_image_subscription(
                topic, lambda message, name=topic: self._on_message(name, message)
            )
            for topic in self.topics
        ]

    def _on_message(self, topic: str, message: Any) -> None:
        if not _valid_image(message):
            return
        now = time.monotonic()
        with self.lock:
            item = self.state[topic]
            previous = item["window_last_message_at"]
            if previous is not None:
                item["window_max_gap_seconds"] = max(
                    item["window_max_gap_seconds"], now - previous
                )
            if item["window_first_message_at"] is None:
                item["window_first_message_at"] = now
            item["window_last_message_at"] = now
            item["window_message_count"] += 1
            item["message_count"] += 1
            item["last_message_at"] = now
            item["latest_message"] = message
            item["width"] = int(getattr(message, "width", 0) or 0)
            item["height"] = int(getattr(message, "height", 0) or 0)
            item["encoding"] = str(getattr(message, "encoding", "") or "")
            item["data_size"] = len(getattr(message, "data", b"") or b"")

    def reset_window(self) -> None:
        now = time.monotonic()
        with self.lock:
            for item in self.state.values():
                item["window_started_at"] = now
                item["window_first_message_at"] = None
                item["window_last_message_at"] = None
                item["window_max_gap_seconds"] = 0.0
                item["window_message_count"] = 0

    def topic_is_quiet(self, topic: str, quiet_seconds: float) -> bool:
        now = time.monotonic()
        with self.lock:
            item = self.state[topic]
            reference = item["window_last_message_at"] or item["window_started_at"]
            return now - reference >= quiet_seconds

    def topics_are_stable(
        self, topics: Sequence[str], stable_seconds: float, max_gap_seconds: float
    ) -> bool:
        if not topics:
            return True
        now = time.monotonic()
        with self.lock:
            for topic in topics:
                item = self.state[topic]
                first = item["window_first_message_at"]
                last = item["window_last_message_at"]
                if first is None or last is None:
                    return False
                if now - first < stable_seconds or now - last > max_gap_seconds:
                    return False
                if item["window_max_gap_seconds"] > max_gap_seconds:
                    return False
        return True

    def latest(self, topic: str) -> Tuple[int, Any]:
        with self.lock:
            item = self.state[topic]
            return int(item["message_count"]), item["latest_message"]

    def snapshot(self, topics: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        selected = list(topics) if topics is not None else self.topics
        now = time.monotonic()
        rows = []
        with self.lock:
            for topic in selected:
                item = self.state[topic]
                first = item["window_first_message_at"]
                last = item["window_last_message_at"]
                rows.append(
                    {
                        "topic": topic,
                        "message_count": item["message_count"],
                        "window_message_count": item["window_message_count"],
                        "seconds_since_last_message": (
                            now - item["last_message_at"]
                            if item["last_message_at"] is not None
                            else None
                        ),
                        "window_stable_seconds": (
                            now - item["window_first_message_at"]
                            if item["window_first_message_at"] is not None
                            else 0.0
                        ),
                        "window_max_gap_seconds": item["window_max_gap_seconds"],
                        "width": item["width"],
                        "height": item["height"],
                        "encoding": item["encoding"],
                        "data_size": item["data_size"],
                    }
                )
        return rows

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


class StreamVerificationError(RuntimeError):
    def __init__(self, message: str, details: Dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


def wait_for_initial_stability(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    stable_seconds: float,
    max_gap_seconds: float,
    timeout: float,
) -> List[Dict[str, Any]]:
    monitor.reset_window()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.1)
        if monitor.topics_are_stable(monitor.topics, stable_seconds, max_gap_seconds):
            return monitor.snapshot()
    raise StreamVerificationError(
        f"baseline image streams were not stable within {timeout:.1f}s",
        {"elapsed_seconds": timeout, "topics": monitor.snapshot()},
    )


def wait_for_disabled_state(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    target_topic: str,
    stop_stable_seconds: float,
    stable_seconds: float,
    max_gap_seconds: float,
    timeout: float,
) -> Dict[str, Any]:
    other_topics = [topic for topic in monitor.topics if topic != target_topic]
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.1)
        target_quiet = monitor.topic_is_quiet(target_topic, stop_stable_seconds)
        others_stable = monitor.topics_are_stable(
            other_topics, stable_seconds, max_gap_seconds
        )
        if target_quiet and others_stable:
            return {
                "elapsed_seconds": time.monotonic() - started,
                "target_quiet": True,
                "other_streams_stable": True,
                "topics": monitor.snapshot(),
            }
    raise StreamVerificationError(
        f"disabled-state verification timed out after {timeout:.1f}s for {target_topic}",
        {
            "elapsed_seconds": time.monotonic() - started,
            "target_quiet": monitor.topic_is_quiet(target_topic, stop_stable_seconds),
            "other_streams_stable": monitor.topics_are_stable(
                other_topics, stable_seconds, max_gap_seconds
            ),
            "topics": monitor.snapshot(),
        },
    )


def wait_for_all_disabled_state(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    stop_stable_seconds: float,
    timeout: float,
) -> Dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.1)
        quiet_topics = [
            topic
            for topic in monitor.topics
            if monitor.topic_is_quiet(topic, stop_stable_seconds)
        ]
        if len(quiet_topics) == len(monitor.topics):
            return {
                "elapsed_seconds": time.monotonic() - started,
                "all_streams_quiet": True,
                "topics": monitor.snapshot(),
            }
    quiet_topics = [
        topic
        for topic in monitor.topics
        if monitor.topic_is_quiet(topic, stop_stable_seconds)
    ]
    raise StreamVerificationError(
        f"all-stream disabled-state verification timed out after {timeout:.1f}s",
        {
            "elapsed_seconds": time.monotonic() - started,
            "all_streams_quiet": len(quiet_topics) == len(monitor.topics),
            "quiet_topics": quiet_topics,
            "topics": monitor.snapshot(),
        },
    )


def wait_for_enabled_state(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    stable_seconds: float,
    max_gap_seconds: float,
    timeout: float,
) -> Dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.1)
        if monitor.topics_are_stable(monitor.topics, stable_seconds, max_gap_seconds):
            return {
                "elapsed_seconds": time.monotonic() - started,
                "all_streams_stable": True,
                "topics": monitor.snapshot(),
            }
    raise StreamVerificationError(
        f"enabled-state verification timed out after {timeout:.1f}s",
        {
            "elapsed_seconds": time.monotonic() - started,
            "all_streams_stable": monitor.topics_are_stable(
                monitor.topics, stable_seconds, max_gap_seconds
            ),
            "topics": monitor.snapshot(),
        },
    )


def expected_ros_encodings(spec: StreamProfileSpec) -> Tuple[str, ...]:
    stream_format = spec.format.upper()
    if not stream_format or stream_format == "ANY":
        return ()
    is_depth = spec.stream == "depth"
    is_ir = spec.stream in {"ir", "left_ir", "right_ir"}
    if stream_format in {"Y8", "GRAY"}:
        return ("8uc1",) if is_depth else ("mono8",)
    if stream_format in {"Y10", "Y11", "Y12", "Y14", "Y16", "Z16", "RW16"}:
        return ("16uc1",) if is_depth else ("mono16",)
    if stream_format in {"MJPG", "MJPEG"}:
        return ("mono8",) if is_ir else ("rgb8",)
    if stream_format == "BGR":
        return ("bgr8",)
    if stream_format in {"RGB", "RGB888"}:
        return ("rgb8",)
    if stream_format == "BGRA":
        return ("bgra8",)
    if stream_format == "RGBA":
        return ("rgba8",)
    if stream_format in {"YUYV", "YUYV2", "UYVY", "I420", "NV12", "NV21"}:
        return ("rgb8",)
    return ()


def evaluate_profile_state(
    specs: Sequence[StreamProfileSpec],
    snapshot: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    rows_by_topic = {str(row.get("topic", "")): row for row in snapshot}
    checks = []
    for spec in specs:
        row = rows_by_topic.get(spec.topic, {})
        actual_width = int(row.get("width", 0) or 0)
        actual_height = int(row.get("height", 0) or 0)
        actual_encoding = str(row.get("encoding", "") or "").lower()
        resolution_match = actual_width == spec.width and actual_height == spec.height
        expected_encodings = expected_ros_encodings(spec)
        encoding_match = not expected_encodings or actual_encoding in expected_encodings
        checks.append(
            {
                "topic": spec.topic,
                "stream": spec.stream,
                "expected_width": spec.width,
                "expected_height": spec.height,
                "expected_fps": spec.fps,
                "expected_format": spec.format,
                "expected_ros_encodings": list(expected_encodings),
                "actual_width": actual_width,
                "actual_height": actual_height,
                "actual_encoding": actual_encoding,
                "resolution_match": resolution_match,
                "format_encoding_check_supported": bool(expected_encodings),
                "format_encoding_match": encoding_match,
                "passed": resolution_match and encoding_match,
            }
        )
    return {
        "all_profiles_match": bool(checks) and all(item["passed"] for item in checks),
        "profiles": checks,
    }


def wait_for_profile_state(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    specs: Sequence[StreamProfileSpec],
    stable_seconds: float,
    max_gap_seconds: float,
    timeout: float,
) -> Dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout
    last_state: Dict[str, Any] = {"all_profiles_match": False, "profiles": []}
    while time.monotonic() < deadline:
        session.assert_running()
        harness.spin_once(0.1)
        if not monitor.topics_are_stable(
            monitor.topics, stable_seconds, max_gap_seconds
        ):
            continue
        snapshot = monitor.snapshot()
        last_state = evaluate_profile_state(specs, snapshot)
        if last_state["all_profiles_match"]:
            return {
                "elapsed_seconds": time.monotonic() - started,
                **last_state,
                "topics": snapshot,
            }
    raise StreamVerificationError(
        f"stream-profile verification timed out after {timeout:.1f}s",
        {
            "elapsed_seconds": time.monotonic() - started,
            **last_state,
            "topics": monitor.snapshot(),
        },
    )


def verify_profile_snapshot(
    specs: Sequence[StreamProfileSpec],
    snapshot: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    state = evaluate_profile_state(specs, snapshot)
    if not state["all_profiles_match"]:
        raise StreamVerificationError(
            "stream profile changed or did not recover after toggle",
            {**state, "topics": list(snapshot)},
        )
    return state


class ProfileCallError(RuntimeError):
    def __init__(self, message: str, attempts: List[Dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


def call_profile_with_retry(
    *,
    session: LaunchSession,
    harness: RosHarness,
    group: StreamProfileGroup,
    timeout: float,
    retry_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    attempts = []
    for attempt_index in (1, 2):
        session.assert_running()
        started = time.monotonic()
        try:
            response = harness.call_set_stream_profile(
                group.service, group.profiles, timeout
            )
            success = bool(response.get("success"))
            message = str(response.get("message", ""))
            error = "" if success else (message or "service returned success=false")
        except Exception as exc:  # noqa: BLE001
            success = False
            message = ""
            error = str(exc)
        attempts.append(
            {
                "attempt": attempt_index,
                "success": success,
                "message": message,
                "error": error,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        if success:
            return {
                "success": True,
                "retried": attempt_index > 1,
                "attempts": attempts,
            }
        if attempt_index == 1:
            sleep(retry_delay)
    raise ProfileCallError(
        f"failed to set stream profiles for {group.camera_namespace} after 2 service "
        f"attempts: {attempts[-1]['error']}",
        attempts,
    )


def apply_profile_set(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    groups: Sequence[StreamProfileGroup],
    specs: Sequence[StreamProfileSpec],
    label: str,
    cycle_index: int,
    service_timeout: float,
    retry_delay: float,
    stable_seconds: float,
    max_gap_seconds: float,
    stream_timeout: float,
    warnings: List[Dict[str, Any]],
    emit: StatusLogger,
) -> Dict[str, Any]:
    service_results = []
    for group in groups:
        call = call_profile_with_retry(
            session=session,
            harness=harness,
            group=group,
            timeout=service_timeout,
            retry_delay=retry_delay,
        )
        call["camera_namespace"] = group.camera_namespace
        call["service"] = group.service
        service_results.append(call)
        if call["retried"]:
            warning = {
                "cycle": cycle_index,
                "camera_namespace": group.camera_namespace,
                "action": "set-stream-profile",
                "profile_set": label,
                "message": (
                    f"cycle {cycle_index} {group.camera_namespace}: profile set {label} "
                    "succeeded only after retry"
                ),
            }
            warnings.append(warning)
            emit(warning["message"])
    monitor.reset_window()
    state = wait_for_profile_state(
        session=session,
        harness=harness,
        monitor=monitor,
        specs=specs,
        stable_seconds=stable_seconds,
        max_gap_seconds=max_gap_seconds,
        timeout=stream_timeout,
    )
    return {
        "profile_set": label,
        "services": service_results,
        "verification": state,
    }


class ToggleCallError(RuntimeError):
    def __init__(self, message: str, *, enabled: bool, attempts: List[Dict[str, Any]]) -> None:
        super().__init__(message)
        self.enabled = enabled
        self.attempts = attempts


def call_toggle_with_retry(
    *,
    session: LaunchSession,
    harness: RosHarness,
    target: Any,
    enabled: bool,
    timeout: float,
    retry_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    attempts = []
    for attempt_index in (1, 2):
        session.assert_running()
        started = time.monotonic()
        try:
            response = harness.call_set_bool(target.service, enabled, timeout)
            success = bool(response.get("success"))
            message = str(response.get("message", ""))
            error = "" if success else (message or "service returned success=false")
        except Exception as exc:  # noqa: BLE001
            success = False
            message = ""
            error = str(exc)
        attempts.append(
            {
                "attempt": attempt_index,
                "success": success,
                "message": message,
                "error": error,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        if success:
            return {
                "success": True,
                "retried": attempt_index > 1,
                "attempts": attempts,
            }
        if attempt_index == 1:
            sleep(retry_delay)
    action = "enable" if enabled else "disable"
    raise ToggleCallError(
        f"failed to {action} {target.topic} after 2 service attempts: "
        f"{attempts[-1]['error']}",
        enabled=enabled,
        attempts=attempts,
    )


def best_effort_restore(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    target: StreamTarget,
    service_timeout: float,
    confirmation_timeout: float,
    emit: StatusLogger,
) -> Dict[str, Any]:
    outcome: Dict[str, Any] = {"attempted": True, "success": False, "message": ""}
    try:
        session.assert_running()
        response = harness.call_set_bool(target.service, True, service_timeout)
        if not response.get("success"):
            raise RuntimeError(response.get("message") or "restore returned success=false")
        baseline_sequence, _ = monitor.latest(target.topic)
        deadline = time.monotonic() + confirmation_timeout
        while time.monotonic() < deadline:
            session.assert_running()
            harness.spin_once(0.1)
            current_sequence, _ = monitor.latest(target.topic)
            if current_sequence > baseline_sequence:
                outcome.update(success=True, message="target stream restored and confirmed")
                return outcome
        raise RuntimeError("target stream did not resume during cleanup confirmation")
    except Exception as exc:  # noqa: BLE001
        outcome["message"] = str(exc)
        emit(f"cleanup restore failed for {target.topic}: {exc}")
        return outcome


def best_effort_restore_groups(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: StreamMonitor,
    groups: Sequence[StreamGroupTarget],
    service_timeout: float,
    confirmation_timeout: float,
    emit: StatusLogger,
) -> Dict[str, Any]:
    baselines = {topic: monitor.latest(topic)[0] for topic in monitor.topics}
    outcomes = []
    service_success = True
    for group in groups:
        try:
            session.assert_running()
            response = harness.call_set_bool(group.service, True, service_timeout)
            success = bool(response.get("success"))
            message = str(response.get("message", ""))
            if not success:
                service_success = False
            outcomes.append(
                {
                    "camera_namespace": group.camera_namespace,
                    "service": group.service,
                    "success": success,
                    "message": message,
                }
            )
        except Exception as exc:  # noqa: BLE001
            service_success = False
            outcomes.append(
                {
                    "camera_namespace": group.camera_namespace,
                    "service": group.service,
                    "success": False,
                    "message": str(exc),
                }
            )
    resumed_topics = set()
    deadline = time.monotonic() + confirmation_timeout
    try:
        while service_success and time.monotonic() < deadline:
            session.assert_running()
            harness.spin_once(0.1)
            for topic, baseline in baselines.items():
                if monitor.latest(topic)[0] > baseline:
                    resumed_topics.add(topic)
            if len(resumed_topics) == len(baselines):
                return {
                    "attempted": True,
                    "success": True,
                    "services": outcomes,
                    "resumed_topics": sorted(resumed_topics),
                    "message": "all stream groups restored and confirmed",
                }
    except Exception as exc:  # noqa: BLE001
        outcomes.append({"success": False, "message": str(exc)})
    message = "one or more all-stream services failed"
    if service_success:
        message = "not all target streams resumed during cleanup confirmation"
    emit(f"all-stream cleanup restore failed: {message}")
    return {
        "attempted": True,
        "success": False,
        "services": outcomes,
        "resumed_topics": sorted(resumed_topics),
        "message": message,
    }


def sanitize_path_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip("/")) or "unknown"


class ImagePathSequence:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self._next_indices: Dict[Tuple[str, str], int] = {}

    def next_path(self, target: SaveImageTarget) -> Path:
        key = (target.camera_name, target.stream)
        directory = self.output_root / sanitize_path_part(target.camera_name) / sanitize_path_part(
            target.stream
        )
        if key not in self._next_indices:
            highest = 0
            if directory.is_dir():
                for path in directory.glob("image_*.*"):
                    match = re.fullmatch(r"image_(\d+)\.(?:png|jpg)", path.name)
                    if match:
                        highest = max(highest, int(match.group(1)))
            self._next_indices[key] = highest + 1
        index = self._next_indices[key]
        self._next_indices[key] = index + 1
        suffix = ".jpg" if target.topic_kind == "compressed" else ".png"
        return directory / f"image_{index:04d}{suffix}"


class ImageSaveMonitor:
    def __init__(self, harness: RosHarness, targets: Sequence[SaveImageTarget]) -> None:
        self.harness = harness
        self.lock = threading.Lock()
        self.state: Dict[str, Dict[str, Any]] = {
            target.topic: {"sequence": 0, "latest_message": None}
            for target in targets
        }
        self.subscriptions = [
            harness.create_image_subscription(
                target.topic,
                lambda message, name=target.topic: self._on_message(name, message),
                topic_kind=target.topic_kind,
            )
            for target in targets
        ]

    def _on_message(self, topic: str, message: Any) -> None:
        with self.lock:
            item = self.state[topic]
            item["sequence"] += 1
            item["latest_message"] = message

    def latest(self, topic: str) -> Tuple[int, Any]:
        with self.lock:
            item = self.state[topic]
            return int(item["sequence"]), item["latest_message"]

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


class ImageWriter:
    def __init__(self, output_root: Path) -> None:
        self.cv2 = None
        self.bridge = None
        self.paths = ImagePathSequence(output_root)

    def _ensure_cv_tools(self):
        if self.cv2 is not None and self.bridge is not None:
            return self.bridge, self.cv2
        try:
            import cv2
            from cv_bridge import CvBridge
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "saving raw images as PNG requires cv_bridge and OpenCV Python modules. "
                "Source the camera driver environment or set --save-image-count 0 "
                f"to disable image saving. Original error: {exc}"
            ) from exc
        self.cv2 = cv2
        self.bridge = CvBridge()
        return self.bridge, self.cv2

    def write(self, target: SaveImageTarget, message: Any) -> Dict[str, Any]:
        path = self.paths.next_path(target)
        ensure_dir(path.parent)
        if target.topic_kind == "compressed":
            path.write_bytes(bytes(getattr(message, "data", b"") or b""))
            return {
                "path": str(path),
                "topic": target.topic,
                "topic_kind": target.topic_kind,
            }
        bridge, cv2 = self._ensure_cv_tools()
        encoding = str(getattr(message, "encoding", "") or "")
        image = bridge.imgmsg_to_cv2(message, desired_encoding="passthrough")
        if encoding.lower() == "rgb8":
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif encoding.lower() == "rgba8":
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA)
        ok = cv2.imwrite(
            str(path), image, [int(cv2.IMWRITE_PNG_COMPRESSION), 1]
        )
        if not ok:
            raise RuntimeError(f"failed to write PNG image: {path}")
        return {
            "path": str(path),
            "topic": target.topic,
            "topic_kind": target.topic_kind,
        }


def save_target_images(
    *,
    session: LaunchSession,
    harness: RosHarness,
    monitor: ImageSaveMonitor,
    writer: ImageWriter,
    targets: Sequence[SaveImageTarget],
    count: int,
    timeout: float,
) -> List[Dict[str, Any]]:
    if count <= 0 or not targets:
        return []
    saved: List[Dict[str, Any]] = []
    saved_counts = {target.topic: 0 for target in targets}
    last_sequences = {
        target.topic: monitor.latest(target.topic)[0] for target in targets
    }
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline and any(
        saved_counts[target.topic] < count for target in targets
    ):
        session.assert_running()
        harness.spin_once(0.1)
        for target in targets:
            if saved_counts[target.topic] >= count:
                continue
            sequence, message = monitor.latest(target.topic)
            if sequence <= last_sequences[target.topic] or message is None:
                continue
            last_sequences[target.topic] = sequence
            try:
                saved.append(writer.write(target, message))
                saved_counts[target.topic] += 1
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
    if any(saved_counts[target.topic] != count for target in targets):
        suffix = f"; last error: {last_error}" if last_error else ""
        counts = ", ".join(
            f"{target.topic}={saved_counts[target.topic]}/{count}" for target in targets
        )
        raise RuntimeError(
            f"image saving incomplete within {timeout:.1f}s: {counts}{suffix}"
        )
    return saved


def camera_launch_args(camera: Optional[Dict[str, str]]) -> Dict[str, str]:
    if camera is None:
        return {}
    launch_args: Dict[str, str] = {}
    mappings = {
        "name": "camera_name",
        "serial_number": "serial_number",
        "usb_port": "usb_port",
        "device_ip": "net_device_ip",
        "device_port": "net_device_port",
        "config_file_path": "config_file_path",
    }
    for source, target in mappings.items():
        value = camera.get(source, "")
        if value:
            launch_args[target] = value
    return launch_args


def build_summary(result: Dict[str, Any]) -> str:
    command_text = " ".join(
        shlex.quote(str(item)) for item in result.get("command", [])
    )
    lines = [
        "# Stream Toggle Stress Test",
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
        f"- Toggle mode: {result.get('toggle_mode', 'individual')}",
        f"- Stream profile switching: "
        f"{'enabled' if result.get('profile_switch_enabled') else 'disabled'}",
        f"- Initial profile set: {result.get('initial_profile_set', 'disabled')}",
        f"- Last active profile set: {result.get('active_profile_set', '') or 'none'}",
        f"- Stream off seconds: {result.get('stream_off_seconds', 4)}",
        f"- Stream on/preview seconds: {result.get('stream_on_preview_seconds', 4)}",
        f"- Completed cycles: {result.get('completed_cycles', 0)}",
        f"- Completed stream operations: {result.get('completed_operations', 0)}",
        f"- Service retry warnings: {len(result.get('warnings', []))}",
        f"- Saved images: {result.get('saved_image_count', 0)}",
        f"- Saved point cloud/IMU plots: {result.get('saved_sensor_plot_count', 0)}",
        f"- Elapsed seconds: {float(result.get('elapsed_seconds', 0.0) or 0.0):.1f}",
        "",
        "## Target Streams",
        "",
    ]
    targets = result.get("targets", [])
    if targets:
        for target in targets:
            lines.append(f"- `{target.get('topic', '')}` → `{target.get('service', '')}`")
    else:
        lines.append("- None")
    lines.extend(["", "## Point Cloud Topics", ""])
    lines.extend(f"- `{topic}`" for topic in result.get("point_cloud_topics", []))
    if not result.get("point_cloud_topics"):
        lines.append("- None")
    lines.extend(["", "## IMU Topics", ""])
    lines.extend(f"- `{topic}`" for topic in result.get("imu_topics", []))
    if not result.get("imu_topics"):
        lines.append("- None")
    skipped = result.get("skipped_image_topics", [])
    if skipped:
        lines.extend(["", "## Skipped Image Topics", ""])
        for item in skipped:
            lines.append(f"- `{item.get('topic', '')}`: {item.get('reason', '')}")
    if result.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in result["warnings"]:
            lines.append(f"- {warning.get('message', warning)}")
    if result.get("error"):
        lines.extend(["", "## Error", "", str(result["error"])])
    return "\n".join(lines) + "\n"


def validate_args(args) -> Dict[str, Any]:
    if not args.launch_file:
        raise ValueError("--launch-file is required")
    duration_text = str(args.duration or "").strip()
    run_count = args.run_count
    if not duration_text and run_count is None:
        raise ValueError("at least one of --duration or --run-count is required")
    if run_count is not None and run_count <= 0:
        raise ValueError("--run-count must be > 0")
    duration = parse_duration(duration_text, 0.0) if duration_text else None
    if len(args.camera) > 1:
        raise ValueError("stream toggle stress test accepts at most one --camera")
    camera = parse_camera(args.camera[0]) if args.camera else None
    template_camera = camera["name"] if camera else ""
    explicit_topics = []
    for raw_topic in args.image_topic:
        topic = str(raw_topic or "").strip()
        if not topic:
            continue
        if ("{camera}" in topic or "${camera}" in topic) and not camera:
            raise ValueError("image topic {camera} placeholder requires one --camera")
        explicit_topics.append(
            normalize_topic(expand_camera_topic(topic, template_camera))
        )
    if len(set(explicit_topics)) != len(explicit_topics):
        raise ValueError("--image-topic contains duplicate topics")
    save_image_topics = []
    for raw_topic in args.save_image_topic:
        topic = str(raw_topic or "").strip()
        if not topic:
            continue
        if ("{camera}" in topic or "${camera}" in topic) and not camera:
            raise ValueError("save image topic {camera} placeholder requires one --camera")
        normalized = normalize_topic(expand_camera_topic(topic, template_camera))
        save_image_target_from_topic(normalized)
        save_image_topics.append(normalized)
    if len(set(save_image_topics)) != len(save_image_topics):
        raise ValueError("--save-image-topic contains duplicate topics")
    if explicit_topics:
        selected_topics = set(explicit_topics)
        unmatched = [
            topic
            for topic in save_image_topics
            if save_image_target_from_topic(topic).target_topic not in selected_topics
        ]
        if unmatched:
            raise ValueError(
                "--save-image-topic does not match a selected --image-topic: "
                + ", ".join(unmatched)
            )
    profile_switch_enabled = args.switch_stream_profile == 1
    profile_sets: Dict[str, List[StreamProfileSpec]] = {"A": [], "B": []}
    for label, raw_specs in (
        ("A", args.stream_profile_a),
        ("B", args.stream_profile_b),
    ):
        if not camera and any(
            "{camera}" in raw or "${camera}" in raw for raw in raw_specs
        ):
            raise ValueError(
                f"stream profile set {label} {{camera}} placeholder requires one --camera"
            )
        parsed_specs = [
            parse_stream_profile_spec(raw, template_camera) for raw in raw_specs
        ]
        topics = [spec.topic for spec in parsed_specs]
        if len(set(topics)) != len(topics):
            raise ValueError(f"--stream-profile-{label.lower()} contains duplicate topics")
        profile_sets[label] = parsed_specs
    if not profile_switch_enabled and (profile_sets["A"] or profile_sets["B"]):
        raise ValueError(
            "--stream-profile-a/--stream-profile-b require --switch-stream-profile 1"
        )
    if profile_switch_enabled:
        if not profile_sets["A"] or not profile_sets["B"]:
            raise ValueError(
                "--switch-stream-profile 1 requires both --stream-profile-a and "
                "--stream-profile-b"
            )
        topics_a = {spec.topic for spec in profile_sets["A"]}
        topics_b = {spec.topic for spec in profile_sets["B"]}
        if topics_a != topics_b:
            raise ValueError("stream profile sets A and B must configure the same topics")
        values_a = {
            spec.topic: (spec.width, spec.height, spec.fps, spec.format)
            for spec in profile_sets["A"]
        }
        values_b = {
            spec.topic: (spec.width, spec.height, spec.fps, spec.format)
            for spec in profile_sets["B"]
        }
        if values_a == values_b:
            raise ValueError("stream profile sets A and B must be different")
        specs_a_by_topic = {spec.topic: spec for spec in profile_sets["A"]}
        specs_b_by_topic = {spec.topic: spec for spec in profile_sets["B"]}
        namespaces = sorted({spec.camera_namespace for spec in profile_sets["A"]})
        for namespace in namespaces:
            namespace_topics = [
                spec.topic
                for spec in profile_sets["A"]
                if spec.camera_namespace == namespace
            ]
            distinguishable = False
            for topic in namespace_topics:
                spec_a = specs_a_by_topic[topic]
                spec_b = specs_b_by_topic[topic]
                if (spec_a.width, spec_a.height, spec_a.fps) != (
                    spec_b.width,
                    spec_b.height,
                    spec_b.fps,
                ):
                    distinguishable = True
                    break
                encodings_a = expected_ros_encodings(spec_a)
                encodings_b = expected_ros_encodings(spec_b)
                if encodings_a and encodings_b and encodings_a != encodings_b:
                    distinguishable = True
                    break
            if not distinguishable:
                raise ValueError(
                    f"profile sets A and B for {namespace} cannot be distinguished from "
                    "sensor_msgs/Image; change resolution/FPS or use formats with different "
                    "ROS encodings"
                )
    if args.save_image_count < 0:
        raise ValueError("--save-image-count must be >= 0")
    if args.queue_size <= 0:
        raise ValueError("--queue-size must be > 0")
    retry_delay = float(args.service_retry_delay)
    if retry_delay < 0:
        raise ValueError("--service-retry-delay must be >= 0")
    return {
        "camera": camera,
        "explicit_topics": explicit_topics,
        "save_image_topics": save_image_topics,
        "duration": duration,
        "discovery_timeout": parse_duration(args.topic_discovery_timeout, 30.0),
        "discovery_settle": parse_duration(args.topic_discovery_settle, 2.0),
        "stream_off": parse_duration(args.stream_off_seconds, 4.0),
        "stream_on_preview": parse_duration(args.stream_on_preview_seconds, 4.0),
        "stream_timeout": parse_duration(args.stream_timeout, 20.0),
        "max_gap": parse_duration(args.max_gap_seconds, 1.5),
        "service_timeout": parse_duration(args.service_timeout, 15.0),
        "service_retry_delay": retry_delay,
        "save_image_timeout": parse_duration(args.save_image_timeout, 30.0),
        "profile_switch_enabled": profile_switch_enabled,
        "profile_sets": profile_sets,
    }


def run(args) -> int:
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    config = validate_args(args)
    runtime_env = prepare_runtime_env(args)
    apply_python_paths(runtime_env)
    environment = collect_test_environment(args)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_stream_toggle")
    started_at = iso_now()
    default_results_dir = Path(__file__).resolve().parent / "results" / run_id
    results_dir = ensure_dir(
        Path(args.results_dir).resolve() if args.results_dir else default_results_dir
    )
    install_terminal_log(results_dir / "terminal.log")
    logs_dir = ensure_dir(results_dir / "logs")
    events = EventWriter(results_dir / "events.jsonl")
    emit = StatusLogger(events)

    camera = config["camera"]
    launch_args = dict(DEFAULT_STRESS_LAUNCH_ARGS)
    launch_args.update(camera_launch_args(camera))
    if camera:
        launch_args["log_level"] = args.sdk_log_level
        launch_args["log_file_name"] = f"{camera['name']}.log"
    launch_args = merge_launch_arg_overrides(launch_args, args.launch_arg)
    command = build_launch_command(
        ros_version=args.ros_version,
        launch_package=args.launch_package,
        launch_file=args.launch_file,
        launch_args=launch_args,
    )

    result: Dict[str, Any] = {
        "status": "passed",
        "tool_version": TOOL_VERSION,
        "environment": environment,
        "ros_version": args.ros_version,
        "command": command,
        "launch_file": args.launch_file,
        "launch_package": args.launch_package,
        "launch_args": launch_args,
        "camera": camera,
        "toggle_mode": args.toggle_mode,
        "profile_switch_enabled": config["profile_switch_enabled"],
        "stream_off_seconds": config["stream_off"],
        "stream_on_preview_seconds": config["stream_on_preview"],
        "stream_profile_sets": {
            label: [asdict(spec) for spec in specs]
            for label, specs in config["profile_sets"].items()
        },
        "profile_groups": {},
        "initial_profile_set": "disabled",
        "active_profile_set": "",
        "topic_mode": "manual" if config["explicit_topics"] else "auto",
        "requested_image_topics": config["explicit_topics"],
        "requested_save_image_topics": config["save_image_topics"],
        "requested_point_cloud_topics": list(args.point_cloud_topic),
        "requested_imu_topics": list(args.imu_topic),
        "point_cloud_topics": [],
        "imu_topics": [],
        "save_image_topics": [],
        "targets": [],
        "stream_groups": [],
        "skipped_image_topics": [],
        "duration_limit_seconds": config["duration"],
        "run_count": args.run_count,
        "continue_on_failure": args.continue_on_failure,
        "completed_cycles": 0,
        "completed_operations": 0,
        "saved_image_count": 0,
        "saved_sensor_plot_count": 0,
        "warnings": [],
        "cycles": [],
        "elapsed_seconds": 0.0,
    }
    launch_env = dict(runtime_env)
    launch_env["ORBBEC_LOG_DIR"] = str(ensure_dir(logs_dir / "sdk"))
    session = LaunchSession(
        command=command,
        work_dir=results_dir,
        env=launch_env,
        log_path=logs_dir / "camera.launch.log",
        emit=emit,
    )
    image_writer: Optional[ImageWriter] = None
    image_save_monitor: Optional[ImageSaveMonitor] = None
    target_may_be_disabled = False
    groups_may_be_disabled = False
    monitor: Optional[StreamMonitor] = None
    test_started_monotonic = time.monotonic()
    deadline = (
        test_started_monotonic + config["duration"]
        if config["duration"] is not None
        else None
    )

    try:
        if args.save_image_count > 0:
            image_writer = ImageWriter(results_dir / "images")
        emit("test started", event="phase", phase="starting")
        emit(f"results dir: {results_dir}")
        emit("launch command: " + " ".join(shlex.quote(item) for item in command))
        session.start()
        with RosHarness(
            args.ros_version,
            "stream_toggle_stress_test",
            args.queue_size,
            enable_profile_switch=config["profile_switch_enabled"],
        ) as harness:
            targets, skipped = discover_stream_targets(
                session=session,
                harness=harness,
                explicit_topics=config["explicit_topics"],
                timeout=config["discovery_timeout"],
                settle_seconds=config["discovery_settle"],
            )
            result["targets"] = [asdict(target) for target in targets]
            result["skipped_image_topics"] = skipped
            save_targets_by_topic: Dict[str, List[SaveImageTarget]] = {
                target.topic: [] for target in targets
            }
            if image_writer is not None:
                save_targets_by_topic = build_save_image_targets(
                    targets,
                    config["save_image_topics"],
                    harness.get_topic_names_and_types(),
                )
                all_save_targets = [
                    save_target
                    for target in targets
                    for save_target in save_targets_by_topic[target.topic]
                ]
                result["save_image_topics"] = [
                    asdict(save_target) for save_target in all_save_targets
                ]
                image_save_monitor = ImageSaveMonitor(harness, all_save_targets)
            service_types = harness.get_service_names_and_types()
            groups: List[StreamGroupTarget] = []
            if args.toggle_mode == "all":
                groups = build_stream_groups(targets, service_types)
                result["stream_groups"] = [asdict(group) for group in groups]
                emit(
                    "all-stream services: "
                    + ", ".join(group.service for group in groups)
                )
            profile_groups: Dict[str, List[StreamProfileGroup]] = {"A": [], "B": []}
            if config["profile_switch_enabled"]:
                for label in ("A", "B"):
                    profile_groups[label] = build_profile_groups(
                        config["profile_sets"][label], targets, service_types
                    )
                result["profile_groups"] = {
                    label: [asdict(group) for group in profile_groups[label]]
                    for label in ("A", "B")
                }
                emit(
                    "stream profile switching enabled: "
                    + ", ".join(
                        spec.topic for spec in config["profile_sets"]["A"]
                    )
                )
            emit(
                "target streams: " + ", ".join(target.topic for target in targets),
                event="phase",
                phase="preflight",
            )
            for item in skipped:
                emit(f"skip image topic {item['topic']}: {item['reason']}")
            monitor = StreamMonitor(harness, [target.topic for target in targets])
            result["baseline"] = wait_for_initial_stability(
                session=session,
                harness=harness,
                monitor=monitor,
                stable_seconds=config["stream_on_preview"],
                max_gap_seconds=config["max_gap"],
                timeout=config["stream_timeout"],
            )
            camera_names = sorted({target.camera_name for target in targets})
            configured_point_cloud_topics = expand_topic_templates(
                args.point_cloud_topic, camera_names
            )
            configured_imu_topics = expand_topic_templates(
                args.imu_topic, camera_names
            )
            point_cloud_topics, imu_topics, sensor_topic_cameras = (
                discover_sensor_topics(
                    harness=harness,
                    camera_names=camera_names,
                    point_cloud_topics=configured_point_cloud_topics,
                    imu_topics=configured_imu_topics,
                    timeout=config["discovery_timeout"],
                    ensure_running=session.assert_running,
                    settle_seconds=config["discovery_settle"],
                )
            )
            result["point_cloud_topics"] = point_cloud_topics
            result["imu_topics"] = imu_topics
            emit(
                f"sensor baseline: {len(point_cloud_topics)} point cloud, "
                f"{len(imu_topics)} IMU topic(s)"
            )

            def capture_on_state_sensors():
                sensor_timeout = max(
                    config["save_image_timeout"],
                    2.0 * max(args.save_image_count, 1) + 5.0,
                )
                ok, snapshot, message = capture_sensor_artifacts(
                    harness=harness,
                    point_cloud_topics=point_cloud_topics,
                    imu_topics=imu_topics,
                    topic_cameras=sensor_topic_cameras,
                    output_root=results_dir / "images",
                    save_count=args.save_image_count,
                    timeout=sensor_timeout,
                    ensure_running=session.assert_running,
                )
                if not ok:
                    raise StreamVerificationError(message, {"sensors": snapshot})
                result["saved_sensor_plot_count"] += sum(
                    len(row.get("files", [])) for row in snapshot
                )
                return snapshot
            profile_sequence = ("A", "B")
            if config["profile_switch_enabled"]:
                initial_a = evaluate_profile_state(
                    config["profile_sets"]["A"], result["baseline"]
                )["all_profiles_match"]
                initial_b = evaluate_profile_state(
                    config["profile_sets"]["B"], result["baseline"]
                )["all_profiles_match"]
                if initial_a and not initial_b:
                    result["initial_profile_set"] = "A"
                    profile_sequence = ("B", "A")
                elif initial_b and not initial_a:
                    result["initial_profile_set"] = "B"
                    profile_sequence = ("A", "B")
                else:
                    result["initial_profile_set"] = "unknown"
            emit("all baseline streams are stable", event="phase", phase="running")

            cycle_index = 0
            stop_requested = False
            while not stop_requested:
                if args.run_count is not None and cycle_index >= args.run_count:
                    break
                if (
                    cycle_index > 0
                    and deadline is not None
                    and time.monotonic() >= deadline
                ):
                    break
                cycle_index += 1
                cycle = {
                    "cycle": cycle_index,
                    "status": "running",
                    "started_at": iso_now(),
                    "ended_at": "",
                    "operations": [],
                }
                result["cycles"].append(cycle)
                active_profile_specs: Sequence[StreamProfileSpec] = ()
                if config["profile_switch_enabled"]:
                    profile_label = profile_sequence[(cycle_index - 1) % 2]
                    active_profile_specs = config["profile_sets"][profile_label]
                    emit(
                        f"cycle {cycle_index}: switch to stream profile set {profile_label}",
                        event="progress",
                        current=cycle_index,
                        total=args.run_count,
                        cycle=cycle_index,
                        phase="switching-stream-profile",
                    )
                    try:
                        cycle["profile_switch"] = apply_profile_set(
                            session=session,
                            harness=harness,
                            monitor=monitor,
                            groups=profile_groups[profile_label],
                            specs=active_profile_specs,
                            label=profile_label,
                            cycle_index=cycle_index,
                            service_timeout=config["service_timeout"],
                            retry_delay=config["service_retry_delay"],
                            stable_seconds=config["stream_on_preview"],
                            max_gap_seconds=config["max_gap"],
                            stream_timeout=config["stream_timeout"],
                            warnings=result["warnings"],
                            emit=emit,
                        )
                        cycle["profile_switch"]["status"] = "passed"
                        result["active_profile_set"] = profile_label
                    except ProfileCallError as exc:
                        cycle["profile_switch"] = {
                            "profile_set": profile_label,
                            "status": "failed",
                            "error": str(exc),
                            "attempts": exc.attempts,
                        }
                        cycle["status"] = "failed"
                        cycle["error"] = str(exc)
                        cycle["ended_at"] = iso_now()
                        result["status"] = "failed"
                        result.setdefault("errors", []).append(str(exc))
                        if not args.continue_on_failure:
                            raise
                        emit(
                            f"cycle {cycle_index}: profile switch failed; "
                            "continuing with the next cycle"
                        )
                        continue
                    except StreamVerificationError as exc:
                        cycle["profile_switch"] = {
                            "profile_set": profile_label,
                            "status": "failed",
                            "error": str(exc),
                            "verification_failure": exc.details,
                        }
                        cycle["status"] = "failed"
                        cycle["error"] = str(exc)
                        cycle["ended_at"] = iso_now()
                        result["status"] = "failed"
                        result.setdefault("errors", []).append(str(exc))
                        if not args.continue_on_failure:
                            raise
                        emit(
                            f"cycle {cycle_index}: profile verification failed; "
                            "continuing with the next cycle"
                        )
                        continue
                if args.toggle_mode == "all":
                    operation = {
                        "index": 1,
                        "mode": "all",
                        "topics": [target.topic for target in targets],
                        "services": [group.service for group in groups],
                        "status": "running",
                        "started_at": iso_now(),
                        "ended_at": "",
                        "disable_services": [],
                        "enable_services": [],
                        "images": [],
                        "sensors": [],
                    }
                    cycle["operations"].append(operation)
                    groups_may_be_disabled = True
                    emit(
                        f"cycle {cycle_index}: disable all streams",
                        event="progress",
                        current=cycle_index,
                        total=args.run_count,
                        cycle=cycle_index,
                        phase="disabling-all",
                    )
                    try:
                        for group in groups:
                            disable = call_toggle_with_retry(
                                session=session,
                                harness=harness,
                                target=group,
                                enabled=False,
                                timeout=config["service_timeout"],
                                retry_delay=config["service_retry_delay"],
                            )
                            disable["camera_namespace"] = group.camera_namespace
                            disable["service"] = group.service
                            operation["disable_services"].append(disable)
                            if disable["retried"]:
                                warning = {
                                    "cycle": cycle_index,
                                    "camera_namespace": group.camera_namespace,
                                    "action": "disable-all",
                                    "message": (
                                        f"cycle {cycle_index} {group.camera_namespace}: "
                                        "disable-all succeeded only after retry"
                                    ),
                                }
                                result["warnings"].append(warning)
                                emit(warning["message"])
                        monitor.reset_window()
                        operation["disabled_state"] = wait_for_all_disabled_state(
                            session=session,
                            harness=harness,
                            monitor=monitor,
                            stop_stable_seconds=config["stream_off"],
                            timeout=config["stream_timeout"],
                        )

                        emit(
                            f"cycle {cycle_index}: enable all streams",
                            event="progress",
                            current=cycle_index,
                            total=args.run_count,
                            cycle=cycle_index,
                            phase="enabling-all",
                        )
                        for group in groups:
                            enable = call_toggle_with_retry(
                                session=session,
                                harness=harness,
                                target=group,
                                enabled=True,
                                timeout=config["service_timeout"],
                                retry_delay=config["service_retry_delay"],
                            )
                            enable["camera_namespace"] = group.camera_namespace
                            enable["service"] = group.service
                            operation["enable_services"].append(enable)
                            if enable["retried"]:
                                warning = {
                                    "cycle": cycle_index,
                                    "camera_namespace": group.camera_namespace,
                                    "action": "enable-all",
                                    "message": (
                                        f"cycle {cycle_index} {group.camera_namespace}: "
                                        "enable-all succeeded only after retry"
                                    ),
                                }
                                result["warnings"].append(warning)
                                emit(warning["message"])
                        monitor.reset_window()
                        operation["enabled_state"] = wait_for_enabled_state(
                            session=session,
                            harness=harness,
                            monitor=monitor,
                            stable_seconds=config["stream_on_preview"],
                            max_gap_seconds=config["max_gap"],
                            timeout=config["stream_timeout"],
                        )
                        if active_profile_specs:
                            operation["profile_state_after_toggle"] = verify_profile_snapshot(
                                active_profile_specs,
                                operation["enabled_state"]["topics"],
                            )
                        groups_may_be_disabled = False
                        if image_writer is not None and image_save_monitor is not None:
                            for target in targets:
                                images = save_target_images(
                                    session=session,
                                    harness=harness,
                                    monitor=image_save_monitor,
                                    writer=image_writer,
                                    targets=save_targets_by_topic[target.topic],
                                    count=args.save_image_count,
                                    timeout=config["save_image_timeout"],
                                )
                                operation["images"].extend(images)
                                result["saved_image_count"] += len(images)
                        operation["sensors"] = capture_on_state_sensors()
                        operation["status"] = "passed"
                        result["completed_operations"] += len(targets)
                        emit(
                            f"cycle {cycle_index}: all streams passed",
                            event="progress",
                            current=cycle_index,
                            total=args.run_count,
                            cycle=cycle_index,
                            phase="completed-all-streams",
                        )
                    except BaseException as exc:  # cleanup must run for interruption
                        operation["status"] = (
                            "interrupted" if INTERRUPTED else "failed"
                        )
                        operation["error"] = str(exc)
                        cycle["error"] = str(exc)
                        if isinstance(exc, ToggleCallError):
                            key = "enable_services" if exc.enabled else "disable_services"
                            operation[key].append(
                                {
                                    "success": False,
                                    "retried": True,
                                    "attempts": exc.attempts,
                                }
                            )
                        if isinstance(exc, StreamVerificationError):
                            operation["verification_failure"] = exc.details
                        if groups_may_be_disabled:
                            operation["cleanup_restore"] = best_effort_restore_groups(
                                session=session,
                                harness=harness,
                                monitor=monitor,
                                groups=groups,
                                service_timeout=config["service_timeout"],
                                confirmation_timeout=config["stream_timeout"],
                                emit=emit,
                            )
                            groups_may_be_disabled = False
                        if (
                            isinstance(exc, (KeyboardInterrupt, SystemExit))
                            or INTERRUPTED
                            or not args.continue_on_failure
                        ):
                            raise
                        cycle["status"] = "failed"
                        result["status"] = "failed"
                        result.setdefault("errors", []).append(str(exc))
                        emit(
                            f"cycle {cycle_index}: all-stream operation failed; "
                            "continuing with the next cycle"
                        )
                    finally:
                        operation["ended_at"] = iso_now()
                    cycle["ended_at"] = iso_now()
                    if cycle["status"] == "running":
                        cycle["status"] = "passed"
                        result["completed_cycles"] += 1
                        emit(
                            f"cycle {cycle_index} completed",
                            event="progress",
                            current=cycle_index,
                            total=args.run_count,
                            phase="completed-cycle",
                        )
                    continue
                for target_index, target in enumerate(targets, start=1):
                    if (
                        cycle_index > 1
                        and deadline is not None
                        and time.monotonic() >= deadline
                    ):
                        cycle["status"] = "partial"
                        stop_requested = True
                        break
                    operation = {
                        "index": target_index,
                        "topic": target.topic,
                        "service": target.service,
                        "status": "running",
                        "started_at": iso_now(),
                        "ended_at": "",
                        "images": [],
                        "sensors": [],
                    }
                    cycle["operations"].append(operation)
                    target_may_be_disabled = True
                    emit(
                        f"cycle {cycle_index}, stream {target_index}/{len(targets)}: "
                        f"disable {target.topic}",
                        event="progress",
                        current=target_index,
                        total=len(targets),
                        cycle=cycle_index,
                        phase="disabling",
                    )
                    try:
                        disable = call_toggle_with_retry(
                            session=session,
                            harness=harness,
                            target=target,
                            enabled=False,
                            timeout=config["service_timeout"],
                            retry_delay=config["service_retry_delay"],
                        )
                        operation["disable_service"] = disable
                        if disable["retried"]:
                            warning = {
                                "cycle": cycle_index,
                                "topic": target.topic,
                                "action": "disable",
                                "message": (
                                    f"cycle {cycle_index} {target.topic}: disable succeeded "
                                    "only after retry"
                                ),
                            }
                            result["warnings"].append(warning)
                            emit(warning["message"])
                        monitor.reset_window()
                        operation["disabled_state"] = wait_for_disabled_state(
                            session=session,
                            harness=harness,
                            monitor=monitor,
                            target_topic=target.topic,
                            stop_stable_seconds=config["stream_off"],
                            stable_seconds=config["stream_off"],
                            max_gap_seconds=config["max_gap"],
                            timeout=config["stream_timeout"],
                        )

                        emit(
                            f"cycle {cycle_index}: enable {target.topic}",
                            event="progress",
                            current=target_index,
                            total=len(targets),
                            cycle=cycle_index,
                            phase="enabling",
                        )
                        enable = call_toggle_with_retry(
                            session=session,
                            harness=harness,
                            target=target,
                            enabled=True,
                            timeout=config["service_timeout"],
                            retry_delay=config["service_retry_delay"],
                        )
                        operation["enable_service"] = enable
                        if enable["retried"]:
                            warning = {
                                "cycle": cycle_index,
                                "topic": target.topic,
                                "action": "enable",
                                "message": (
                                    f"cycle {cycle_index} {target.topic}: enable succeeded "
                                    "only after retry"
                                ),
                            }
                            result["warnings"].append(warning)
                            emit(warning["message"])
                        monitor.reset_window()
                        operation["enabled_state"] = wait_for_enabled_state(
                            session=session,
                            harness=harness,
                            monitor=monitor,
                            stable_seconds=config["stream_on_preview"],
                            max_gap_seconds=config["max_gap"],
                            timeout=config["stream_timeout"],
                        )
                        if active_profile_specs:
                            operation["profile_state_after_toggle"] = verify_profile_snapshot(
                                active_profile_specs,
                                operation["enabled_state"]["topics"],
                            )
                        target_may_be_disabled = False
                        if image_writer is not None and image_save_monitor is not None:
                            operation["images"] = save_target_images(
                                session=session,
                                harness=harness,
                                monitor=image_save_monitor,
                                writer=image_writer,
                                targets=save_targets_by_topic[target.topic],
                                count=args.save_image_count,
                                timeout=config["save_image_timeout"],
                            )
                            result["saved_image_count"] += len(operation["images"])
                        operation["sensors"] = capture_on_state_sensors()
                        operation["status"] = "passed"
                        result["completed_operations"] += 1
                        emit(
                            f"cycle {cycle_index}: passed {target.topic}",
                            event="progress",
                            current=target_index,
                            total=len(targets),
                            cycle=cycle_index,
                            phase="completed-stream",
                        )
                    except BaseException as exc:  # cleanup must also run for KeyboardInterrupt
                        operation["status"] = (
                            "interrupted" if INTERRUPTED else "failed"
                        )
                        operation["error"] = str(exc)
                        cycle["error"] = str(exc)
                        if isinstance(exc, ToggleCallError):
                            key = "enable_service" if exc.enabled else "disable_service"
                            operation[key] = {
                                "success": False,
                                "retried": True,
                                "attempts": exc.attempts,
                            }
                        if isinstance(exc, StreamVerificationError):
                            operation["verification_failure"] = exc.details
                        if target_may_be_disabled:
                            operation["cleanup_restore"] = best_effort_restore(
                                session=session,
                                harness=harness,
                                monitor=monitor,
                                target=target,
                                service_timeout=config["service_timeout"],
                                confirmation_timeout=config["stream_timeout"],
                                emit=emit,
                            )
                            target_may_be_disabled = False
                        if (
                            isinstance(exc, (KeyboardInterrupt, SystemExit))
                            or INTERRUPTED
                            or not args.continue_on_failure
                        ):
                            raise
                        cycle["status"] = "failed"
                        result["status"] = "failed"
                        result.setdefault("errors", []).append(str(exc))
                        emit(
                            f"cycle {cycle_index}: {target.topic} failed; "
                            "continuing with the remaining work"
                        )
                    finally:
                        operation["ended_at"] = iso_now()
                cycle["ended_at"] = iso_now()
                if cycle["status"] == "running":
                    cycle["status"] = "passed"
                    result["completed_cycles"] += 1
                    emit(
                        f"cycle {cycle_index} completed",
                        event="progress",
                        current=cycle_index,
                        total=args.run_count,
                        phase="completed-cycle",
                    )
            emit("test limits reached", event="phase", phase="stopping")
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        emit("test interrupted by user")
        if result["cycles"] and result["cycles"][-1]["status"] == "running":
            result["cycles"][-1]["status"] = "interrupted"
            result["cycles"][-1]["ended_at"] = iso_now()
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            result["status"] = "interrupted"
            emit("test interrupted by user")
        else:
            result["status"] = "failed"
            result["error"] = str(exc)
            if isinstance(exc, StreamVerificationError):
                result["verification_failure"] = exc.details
            emit(f"test failed: {exc}")
        if result["cycles"] and result["cycles"][-1]["status"] == "running":
            result["cycles"][-1]["status"] = result["status"]
            result["cycles"][-1]["ended_at"] = iso_now()
    finally:
        if image_save_monitor is not None:
            try:
                image_save_monitor.close()
            except Exception as exc:  # noqa: BLE001
                result.setdefault("cleanup_errors", []).append(str(exc))
        if monitor is not None:
            try:
                monitor.close()
            except Exception as exc:  # noqa: BLE001
                result.setdefault("cleanup_errors", []).append(str(exc))
        emit("stop launch")
        try:
            session.stop()
        except Exception as exc:  # noqa: BLE001
            result.setdefault("cleanup_errors", []).append(str(exc))
            if result["status"] == "passed":
                result["status"] = "failed"
                result["error"] = f"failed to stop launch cleanly: {exc}"
        result["elapsed_seconds"] = time.monotonic() - test_started_monotonic
        (results_dir / "summary.md").write_text(build_summary(result), encoding="utf-8")
        emit(
            f"test finished with status {result['status']}",
            event="completed",
            status=result["status"],
        )
        payload = contract_result(
            test_id=TEST_ID,
            run_id=run_id,
            started_at=started_at,
            ended_at=iso_now(),
            request=namespace_request(args),
            details=result,
            summary={
                "completed_cycles": result["completed_cycles"],
                "completed_operations": result["completed_operations"],
                "warning_count": len(result["warnings"]),
                "saved_image_count": result["saved_image_count"],
                "saved_sensor_plot_count": result["saved_sensor_plot_count"],
            },
            artifacts=artifact_list(results_dir),
        )
        atomic_write_json(results_dir / "result.json", payload)
        signal.signal(signal.SIGINT, previous_sigint_handler)

    if result["status"] == "passed":
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(
        description=(
            "Stress-test individual or all-stream camera toggles, optionally alternate "
            "resolution/FPS/format profiles, and verify recovery."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py "
            "--ros-version 2 --driver-setup /path/to/install/setup.bash "
            "--launch-file gemini_330_series.launch.py --camera name=camera "
            "--duration 1h\n\n"
            "  python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py "
            "--launch-file /path/to/multi_camera.launch.py "
            "--image-topic /camera_01/color/image_raw "
            "--image-topic /camera_02/depth/image_raw --run-count 10\n"
        ),
    )
    parser.add_argument(
        "--ros-version", choices=("1", "2"), default=os.environ.get("ROS_VERSION", "2")
    )
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument(
        "--driver-setup",
        default=os.environ.get(
            "ORBBEC_DRIVER_SETUP", os.environ.get("ORBBEC_CAMERA_SETUP", "")
        ),
    )
    parser.add_argument("--launch-package", default="orbbec_camera")
    parser.add_argument("--launch-file", required=True)
    parser.add_argument(
        "--launch-arg", action="append", default=[], help="Extra launch arg, KEY=VALUE or KEY:=VALUE"
    )
    parser.add_argument(
        "--sdk-log-level",
        choices=("debug", "info", "warn", "error", "fatal", "none"),
        default="debug",
    )
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        help=(
            "Optional single-camera launch arguments as comma-separated KEY=VALUE fields. "
            "Do not use for a preconfigured multi-camera launch."
        ),
    )
    parser.add_argument(
        "--image-topic",
        action="append",
        default=[],
        help=(
            "Required sensor_msgs/Image topic; repeat to select streams. If omitted, "
            "all raw image topics with matching toggle services are auto-discovered."
        ),
    )
    parser.add_argument(
        "--save-image-topic",
        action="append",
        default=[],
        help=(
            "Image topic to save; repeat to save raw PNG and/or the matching "
            "image_raw/compressed payload. Defaults to selected raw image topics."
        ),
    )
    parser.add_argument(
        "--point-cloud-topic",
        action="append",
        default=[],
        help=(
            "PointCloud2 topic to require during each enabled state; can repeat and "
            "supports {camera}. When omitted, topics are discovered before cycling."
        ),
    )
    parser.add_argument(
        "--imu-topic",
        action="append",
        default=[],
        help=(
            "Imu topic to require during each enabled state; can repeat and supports "
            "{camera}. When omitted, topics are discovered before cycling."
        ),
    )
    parser.add_argument(
        "--toggle-mode",
        choices=("individual", "all"),
        default="individual",
        help=(
            "individual toggles one stream at a time; all uses each camera's "
            "set_streams_enable service"
        ),
    )
    parser.add_argument(
        "--switch-stream-profile",
        type=int,
        choices=(0, 1),
        default=0,
        help="0 keeps launch stream profiles; 1 alternates configured profile sets A and B",
    )
    parser.add_argument(
        "--stream-profile-a",
        action="append",
        default=[],
        metavar="TOPIC=WIDTHxHEIGHT@FPS[:FORMAT]",
        help="Profile-set A entry with optional SDK format; repeat for streams/cameras",
    )
    parser.add_argument(
        "--stream-profile-b",
        action="append",
        default=[],
        metavar="TOPIC=WIDTHxHEIGHT@FPS[:FORMAT]",
        help="Profile-set B entry with optional SDK format; repeat for streams/cameras",
    )
    parser.add_argument(
        "--duration",
        default="",
        help="Maximum duration; at least one of --duration or --run-count is required",
    )
    parser.add_argument(
        "--run-count",
        type=int,
        default=None,
        help="Maximum completed cycles; at least one of --duration or --run-count is required",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue with the next cycle after a failed cycle (default: stop)",
    )
    parser.add_argument("--topic-discovery-timeout", default="30")
    parser.add_argument("--topic-discovery-settle", default="2")
    parser.add_argument(
        "--stream-off-seconds",
        "--stop-stable-seconds",
        dest="stream_off_seconds",
        default="4",
        help="Seconds to keep and verify the selected stream(s) off",
    )
    parser.add_argument(
        "--stream-on-preview-seconds",
        "--stable-seconds",
        dest="stream_on_preview_seconds",
        default="4",
        help="Seconds to preview and continuously verify streams after enabling",
    )
    parser.add_argument("--stream-timeout", default="20")
    parser.add_argument("--max-gap-seconds", default="1.5")
    parser.add_argument("--service-timeout", default="15")
    parser.add_argument("--service-retry-delay", default="1")
    parser.add_argument(
        "--save-image-count",
        type=int,
        default=1,
        help=(
            "PNG artifacts per image, point cloud, and IMU topic for each enabled "
            "state; 0 keeps validation but disables saving"
        ),
    )
    parser.add_argument("--save-image-timeout", default="30")
    parser.add_argument("--queue-size", type=int, default=10)
    parser.add_argument("--results-dir", default="")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return parser.parse_args(argv)


def main() -> None:
    try:
        sys.exit(run(parse_args()))
    except ValueError as exc:
        print(f"[{timestamp()}] argument error: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)
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
