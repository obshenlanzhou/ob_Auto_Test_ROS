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
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml
except Exception as exc:  # noqa: BLE001
    yaml = None
    YAML_IMPORT_ERROR = exc
else:
    YAML_IMPORT_ERROR = None


ENV_READY_VAR = "LAUNCH_PARAM_LOAD_STRESS_ENV_READY"
INTERRUPTED = False
TOOL_VERSION = "0.1"


@dataclass
class CameraSpec:
    name: str
    serial_number: str = ""
    usb_port: str = ""
    config_file_path: str = ""


def parse_camera_spec(raw: str) -> "CameraSpec":
    """Parse --camera name[,serial_number=SN][,usb_port=XX][,config_file_path=/path]"""
    text = raw.strip()
    if not text:
        raise ValueError("--camera cannot be empty")
    name = ""
    fields: Dict[str, str] = {}
    for index, part in enumerate(item.strip() for item in text.split(",")):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        elif index == 0:
            name = part
        else:
            raise ValueError(f"unsupported item '{part}' in --camera {raw}")
    name = fields.pop("name", name).strip()
    if not name:
        raise ValueError(f"camera name is required in --camera {raw}")
    unsupported = sorted(set(fields) - {"serial_number", "usb_port", "config_file_path"})
    if unsupported:
        raise ValueError(f"unsupported fields in --camera {raw}: {', '.join(unsupported)}")
    return CameraSpec(
        name=name,
        serial_number=fields.get("serial_number", ""),
        usb_port=fields.get("usb_port", ""),
        config_file_path=fields.get("config_file_path", ""),
    )


PLACEHOLDER_VALUES = {"", "any", "none", "null"}
_LAUNCH_ERROR_RE = re.compile(r"\[(ERROR|FATAL|WARN(?:ING)?)\]|process has died|Traceback \(most recent call last\)")
STREAM_TOPIC_MAP = {
    "enable_color": "/{camera}/color/image_raw",
    "enable_depth": "/{camera}/depth/image_raw",
    "enable_ir": "/{camera}/ir/image_raw",
    "enable_left_ir": "/{camera}/left_ir/image_raw",
    "enable_right_ir": "/{camera}/right_ir/image_raw",
    "enable_left_color": "/{camera}/left_color/image_raw",
    "enable_right_color": "/{camera}/right_color/image_raw",
}
SERVICE_CHECKS = {
    "color_exposure": ("get_color_exposure", "int"),
    "color_gain": ("get_color_gain", "int"),
    "color_white_balance": ("get_white_balance", "int"),
    "depth_exposure": ("get_depth_exposure", "int"),
    "depth_gain": ("get_depth_gain", "int"),
    "ir_exposure": ("get_ir_exposure", "int"),
    "ir_gain": ("get_ir_gain", "int"),
    "left_ir_exposure": ("get_left_ir_exposure", "int"),
    "left_ir_gain": ("get_left_ir_gain", "int"),
    "right_ir_exposure": ("get_right_ir_exposure", "int"),
    "right_ir_gain": ("get_right_ir_gain", "int"),
    "enable_laser": ("get_laser_status", "bool"),
    "enable_ldp": ("get_ldp_status", "bool"),
    "enable_ptp_config": ("get_ptp_config", "bool"),
    "point_cloud_decimation_filter_factor": ("get_point_cloud_decimation", "int"),
}
SERVICE_TYPES = {
    ("1", "int"): "orbbec_camera/GetInt32",
    ("1", "bool"): "orbbec_camera/GetBool",
    ("1", "string"): "orbbec_camera/GetString",
    ("2", "int"): "orbbec_camera_msgs/srv/GetInt32",
    ("2", "bool"): "orbbec_camera_msgs/srv/GetBool",
    ("2", "string"): "orbbec_camera_msgs/srv/GetString",
}


VERIFICATION_DESCRIPTION = """\
Config file verification map

1. Parameter check
   Every top-level YAML key is checked against the ROS driver parameter after launch.
   ROS2: ros2 param get /<camera_name>/ob_camera_node <param>
   ROS1: rosparam get /<camera_name>/<camera_name>/<param>
   The script normalizes bool/int/float/string values before comparing.

2. Topic behavior check
   These stream switch parameters are also checked by receiving image topics:
"""


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StatusLogger:
    def __call__(self, message: str) -> None:
        print(f"[{timestamp()}] {message}", flush=True)


def build_verification_map_text() -> str:
    lines = [VERIFICATION_DESCRIPTION.rstrip()]
    for key, topic in sorted(STREAM_TOPIC_MAP.items()):
        lines.append(f"   - {key}: {topic}")
    lines.extend(
        [
            "",
            "   Expected true: one image message must be received before --topic-timeout.",
            "   Expected false: no image message should be received during the short disabled-stream check.",
            "",
            "3. Getter service check",
            "   These parameters are also checked by read-only getter services when advertised:",
        ]
    )
    for key, (service_suffix, value_kind) in sorted(SERVICE_CHECKS.items()):
        service_type_ros1 = SERVICE_TYPES[("1", value_kind)]
        service_type_ros2 = SERVICE_TYPES[("2", value_kind)]
        lines.append(
            f"   - {key}: /<camera_name>/{service_suffix} "
            f"(ROS1 {service_type_ros1}, ROS2 {service_type_ros2})"
        )
    lines.extend(
        [
            "",
            "   Placeholder values (-1, empty string, ANY, none/null) are skipped at service level.",
            "   Missing getter services are reported as unsupported and do not fail the run.",
            "",
            "4. Result meanings",
            "   passed: expected and observed values/behavior match.",
            "   failed: parameter value, topic behavior, or advertised service readback does not match.",
            "   skipped: YAML key is not declared by the resolved launch file, or service value is a placeholder.",
            "   unsupported: getter service is not advertised by the current driver/device.",
        ]
    )
    return "\n".join(lines) + "\n"


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


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_yaml_file(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError(f"failed to import PyYAML: {YAML_IMPORT_ERROR}")
    if not path.is_file():
        raise FileNotFoundError(f"config yaml not found: {path}")
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config yaml must be a top-level mapping: {path}")
    return data


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


def launch_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_launch_command(
    *,
    ros_version: str,
    launch_package: str,
    launch_file: str,
    launch_args: Dict[str, Any],
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


def run_command(
    command: List[str],
    *,
    env: Dict[str, str],
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(shlex.quote(item) for item in command)}\n"
            f"{result.stdout.strip()}"
        )
    return result


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
        self.process = subprocess.Popen(
            self.command,
            cwd=self.work_dir,
            env=self.env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )

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
        if self.process.poll() is None:
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


def resolve_launch_file_path(
    *,
    ros_version: str,
    launch_package: str,
    launch_file: str,
    env: Dict[str, str],
) -> Optional[Path]:
    launch_path = Path(launch_file).expanduser()
    if launch_path.is_file():
        return launch_path.resolve()
    if launch_path.is_absolute() or launch_path.parent != Path("."):
        return None
    try:
        if ros_version == "1":
            pkg = run_command(["rospack", "find", launch_package], env=env, timeout=10, check=True)
            base = Path(pkg.stdout.strip())
        else:
            prefix = run_command(["ros2", "pkg", "prefix", launch_package], env=env, timeout=10, check=True)
            base = Path(prefix.stdout.strip()) / "share" / launch_package
    except Exception:
        return None
    candidate = base / "launch" / launch_file
    return candidate if candidate.is_file() else None


def discover_supported_params(launch_path: Optional[Path], ros_version: str) -> Tuple[set[str], str]:
    if launch_path is None:
        return set(), "launch file could not be resolved; YAML keys will be checked without declaration filtering"
    try:
        text = launch_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set(), f"failed to read launch file: {launch_path}"
    if ros_version == "1":
        names = set(re.findall(r"<arg\s+name=[\"']([^\"']+)[\"']", text))
    else:
        names = set(re.findall(r"DeclareLaunchArgument\(\s*[\"']([^\"']+)[\"']", text))
    return names, f"parsed {len(names)} launch arguments from {launch_path}"


def normalize_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        raw = value.strip()
        lowered = raw.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"none", "null"}:
            return None
        try:
            int_value = int(raw, 0)
        except ValueError:
            pass
        else:
            return int_value
        try:
            float_value = float(raw)
        except ValueError:
            return raw
        return int(float_value) if float_value.is_integer() else float_value
    return value


def is_placeholder_value(value: Any) -> bool:
    normalized = normalize_value(value)
    if normalized == -1:
        return True
    if isinstance(value, str) and value.strip().lower() in PLACEHOLDER_VALUES:
        return True
    return False


def values_match(expected: Any, actual: Any) -> bool:
    expected_norm = normalize_value(expected)
    actual_norm = normalize_value(actual)
    if isinstance(expected_norm, float) or isinstance(actual_norm, float):
        try:
            return abs(float(expected_norm) - float(actual_norm)) < 0.000001
        except (TypeError, ValueError):
            return False
    return expected_norm == actual_norm


def parse_ros2_param_output(output: str) -> Any:
    text = output.strip()
    if "Parameter not set" in text or "not set" in text:
        raise ValueError(text)
    if " is: " in text:
        text = text.split(" is: ", 1)[1]
    return normalize_value(text)


def _query_param_single(
    *,
    ros_version: str,
    camera_name: str,
    param_name: str,
    env: Dict[str, str],
    timeout: float,
) -> Tuple[bool, Any, str]:
    if ros_version == "1":
        command = ["rosparam", "get", f"/{camera_name}/{camera_name}/{param_name}"]
        result = run_command(command, env=env, timeout=timeout)
        if result.returncode != 0:
            return False, None, result.stdout.strip()
        return True, normalize_value(result.stdout.strip()), ""
    node_name = f"/{camera_name}/{camera_name}"
    result = run_command(["ros2", "param", "get", node_name, param_name], env=env, timeout=timeout)
    if result.returncode != 0:
        return False, None, result.stdout.strip()
    try:
        return True, parse_ros2_param_output(result.stdout), ""
    except ValueError as exc:
        return False, None, str(exc)


def bulk_query_params(
    *,
    ros_version: str,
    camera_name: str,
    env: Dict[str, str],
    timeout: float,
) -> Tuple[bool, Dict[str, Any], str]:
    if yaml is None:
        return False, {}, "PyYAML not available"
    node_path = f"/{camera_name}/{camera_name}"
    if ros_version == "1":
        result = run_command(["rosparam", "get", node_path], env=env, timeout=timeout)
        if result.returncode != 0:
            return False, {}, result.stdout.strip()
        try:
            data = yaml.safe_load(result.stdout)
            if isinstance(data, dict):
                return True, {k: normalize_value(v) for k, v in data.items() if not isinstance(v, (dict, list))}, ""
        except Exception as exc:
            return False, {}, str(exc)
    else:
        result = run_command(["ros2", "param", "dump", node_path], env=env, timeout=timeout)
        if result.returncode != 0:
            return False, {}, result.stdout.strip()
        try:
            data = yaml.safe_load(result.stdout)
            if isinstance(data, dict):
                ros_params = data.get(node_path, {}).get("ros__parameters", {})
                if isinstance(ros_params, dict):
                    return True, {k: normalize_value(v) for k, v in ros_params.items() if not isinstance(v, (dict, list))}, ""
        except Exception as exc:
            return False, {}, str(exc)
    return False, {}, "failed to parse param dump output"


def check_params(
    *,
    yaml_params: Dict[str, Any],
    supported_params: set[str],
    declaration_filter_enabled: bool,
    ros_version: str,
    camera_name: str,
    env: Dict[str, str],
    timeout: float,
    emit: StatusLogger,
) -> List[Dict[str, Any]]:
    bulk_ok, all_params, bulk_err = bulk_query_params(
        ros_version=ros_version,
        camera_name=camera_name,
        env=env,
        timeout=timeout,
    )
    if not bulk_ok:
        emit(f"[PARAM] bulk query failed ({bulk_err}), falling back to per-param queries")

    rows = []
    for key, expected in sorted(yaml_params.items()):
        row = {"name": key, "expected": expected, "actual": None, "status": "pending", "message": ""}
        if declaration_filter_enabled and key not in supported_params:
            row["status"] = "skipped"
            row["message"] = "not declared by launch file"
            emit(f"[PARAM][SKIPPED] {key}: not declared by launch file")
            rows.append(row)
            continue
        if bulk_ok:
            if key in all_params:
                ok, actual, message = True, all_params[key], ""
            else:
                ok, actual, message = False, None, "parameter not found"
        else:
            ok, actual, message = _query_param_single(
                ros_version=ros_version,
                camera_name=camera_name,
                param_name=key,
                env=env,
                timeout=timeout,
            )
        row["actual"] = actual
        if not ok:
            row["status"] = "failed"
            row["message"] = message or "parameter query failed"
            emit(f"[PARAM][FAIL] {key}: {row['message']}")
        elif values_match(expected, actual):
            row["status"] = "passed"
            row["message"] = "matches"
            emit(f"[PARAM][PASS] {key}: {actual!r}")
        else:
            row["status"] = "failed"
            row["message"] = f"expected {expected!r}, got {actual!r}"
            emit(f"[PARAM][FAIL] {key}: {row['message']}")
        rows.append(row)
    return rows


def topic_echo_command(ros_version: str, topic: str) -> List[str]:
    if ros_version == "1":
        return ["rostopic", "echo", "-n", "1", f"{topic}/header"]
    return ["ros2", "topic", "echo", "--once", topic, "--field", "header"]


def topic_has_message(
    *,
    ros_version: str,
    topic: str,
    env: Dict[str, str],
    timeout: float,
) -> Tuple[bool, str]:
    try:
        result = run_command(topic_echo_command(ros_version, topic), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"no message within {timeout:.1f}s"
    return result.returncode == 0, result.stdout.strip()


def check_topics(
    *,
    yaml_params: Dict[str, Any],
    ros_version: str,
    camera_name: str,
    env: Dict[str, str],
    timeout: float,
    emit: StatusLogger,
) -> List[Dict[str, Any]]:
    rows = []
    for key, topic_template in sorted(STREAM_TOPIC_MAP.items()):
        if key not in yaml_params:
            continue
        expected_enabled = bool(normalize_value(yaml_params[key]))
        topic = topic_template.format(camera=camera_name)
        row = {
            "name": key,
            "topic": topic,
            "expected_enabled": expected_enabled,
            "status": "pending",
            "message": "",
        }
        has_message, message = topic_has_message(
            ros_version=ros_version,
            topic=topic,
            env=env,
            timeout=timeout if expected_enabled else min(timeout, 3.0),
        )
        if expected_enabled and has_message:
            row["status"] = "passed"
            row["message"] = "received message"
            emit(f"[TOPIC][PASS] {topic}: received message")
        elif expected_enabled:
            row["status"] = "failed"
            row["message"] = message
            emit(f"[TOPIC][FAIL] {topic}: {message}")
        elif has_message:
            row["status"] = "failed"
            row["message"] = "received message although stream is disabled"
            emit(f"[TOPIC][FAIL] {topic}: disabled but received message")
        else:
            row["status"] = "passed"
            row["message"] = "no message observed while disabled"
            emit(f"[TOPIC][PASS] {topic}: disabled, no message observed")
        rows.append(row)
    return rows


def sanitize_path_part(value: str) -> str:
    text = value.strip().strip("/")
    if not text:
        return "root"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)


class RosImageHarness:
    def __init__(self, ros_version: str, node_name: str, queue_size: int = 10) -> None:
        self.ros_version = str(ros_version)
        self.node_name = node_name
        self.queue_size = queue_size
        self._rclpy = None
        self._rospy = None
        self.node = None
        self.subscriptions: list = []
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

    def create_subscription(self, topic: str, callback):
        if self.ros_version == "2":
            sub = self.node.create_subscription(self.image_type, topic, callback, self.queue_size)
        else:
            sub = self._rospy.Subscriber(topic, self.image_type, callback, queue_size=self.queue_size)
        self.subscriptions.append(sub)
        return sub

    def spin_once(self, timeout_sec: float) -> None:
        if self.ros_version == "2":
            self._rclpy.spin_once(self.node, timeout_sec=timeout_sec)
        else:
            time.sleep(timeout_sec)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ros_version == "2":
            for sub in list(self.subscriptions):
                try:
                    self.node.destroy_subscription(sub)
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
            for sub in list(self.subscriptions):
                try:
                    sub.unregister()
                except Exception:
                    pass


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
        output_root: Path,
        save_images_count: int,
        jpg_quality: int,
    ) -> None:
        self.harness = harness
        self.save_images_count = save_images_count
        self.jpg_quality = jpg_quality
        self.output_root = output_root
        self.state: Dict[str, Dict[str, Any]] = {}
        self._bridge = None
        self._cv2 = None
        for topic in topics:
            self.state[topic] = {"saved_files": [], "first_at": None}
            harness.create_subscription(
                topic,
                lambda msg, t=topic: self._on_message(t, msg),
            )

    def _ensure_cv_tools(self):
        if self._bridge is not None:
            return self._bridge, self._cv2
        try:
            import cv2
            from cv_bridge import CvBridge
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "saving JPG images requires cv_bridge and OpenCV. "
                "Source the camera driver environment or set --save-images-count 0. "
                f"Original error: {exc}"
            ) from exc
        self._bridge = CvBridge()
        self._cv2 = cv2
        return self._bridge, self._cv2

    def _write_jpg(self, message: Any, target_path: Path) -> None:
        ensure_dir(target_path.parent)
        bridge, cv2 = self._ensure_cv_tools()
        encoding = str(getattr(message, "encoding", "") or "")
        image = bridge.imgmsg_to_cv2(message, desired_encoding="passthrough")
        if encoding.lower() == "rgb8":
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif encoding.lower() in {"mono16", "16uc1"}:
            image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX)
            image = image.astype("uint8")
        if not cv2.imwrite(str(target_path), image, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpg_quality]):
            raise RuntimeError(f"failed to write JPG: {target_path}")

    def _on_message(self, topic: str, message: Any) -> None:
        item = self.state[topic]
        if item["first_at"] is None and _valid_image(message):
            item["first_at"] = time.monotonic()
        if self.save_images_count <= 0 or not _valid_image(message):
            return
        if len(item["saved_files"]) >= self.save_images_count:
            return
        topic_dir = self.output_root / sanitize_path_part(topic)
        target = topic_dir / f"image_{len(item['saved_files']) + 1:04d}.jpg"
        self._write_jpg(message, target)
        item["saved_files"].append(str(target))

    def all_done(self) -> bool:
        for item in self.state.values():
            if item["first_at"] is None:
                return False
            if self.save_images_count > 0 and len(item["saved_files"]) < self.save_images_count:
                return False
        return True

    def saved_files(self) -> List[str]:
        files = []
        for item in self.state.values():
            files.extend(item["saved_files"])
        return files


def save_topic_images(
    *,
    ros_version: str,
    camera_name: str,
    yaml_params: Dict[str, Any],
    output_root: Path,
    save_images_count: int,
    jpg_quality: int,
    timeout: float,
    emit: StatusLogger,
) -> List[str]:
    enabled_topics = [
        topic_template.format(camera=camera_name)
        for key, topic_template in STREAM_TOPIC_MAP.items()
        if key in yaml_params and bool(normalize_value(yaml_params[key]))
    ]
    if not enabled_topics:
        return []
    emit(f"[IMAGE] saving up to {save_images_count} image(s) per topic for {camera_name}")
    node_name = f"launch_param_load_stress_{camera_name}".replace("-", "_")
    try:
        with RosImageHarness(ros_version, node_name) as harness:
            monitor = ImageCaptureMonitor(
                harness=harness,
                topics=enabled_topics,
                output_root=output_root,
                save_images_count=save_images_count,
                jpg_quality=jpg_quality,
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not monitor.all_done():
                harness.spin_once(0.1)
            saved = monitor.saved_files()
            emit(f"[IMAGE] saved {len(saved)} image(s) for {camera_name}")
            return saved
    except Exception as exc:  # noqa: BLE001
        emit(f"[IMAGE][WARN] image saving failed for {camera_name}: {exc}")
        return []


def list_services(ros_version: str, env: Dict[str, str], timeout: float) -> set[str]:
    command = ["rosservice", "list"] if ros_version == "1" else ["ros2", "service", "list"]
    result = run_command(command, env=env, timeout=timeout)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def parse_service_data(output: str, value_kind: str) -> Tuple[bool, Any, str]:
    success_match = re.search(r"\bsuccess[=:]\s*(true|false|True|False|0|1)", output)
    if success_match and normalize_value(success_match.group(1)) is False:
        message_match = re.search(r"\bmessage[=:]\s*['\"]?(.+?)['\"]?\s*$", output, re.MULTILINE)
        return False, None, message_match.group(1) if message_match else "service returned success=false"
    data_match = re.search(r"\bdata[=:]\s*([^\n\r,)]+)", output)
    if not data_match:
        return False, None, "response does not contain data field"
    raw_data = data_match.group(1).strip().strip("'\"")
    if value_kind == "bool":
        return True, bool(normalize_value(raw_data)), ""
    return True, normalize_value(raw_data), ""


def call_getter_service(
    *,
    ros_version: str,
    service_name: str,
    value_kind: str,
    env: Dict[str, str],
    timeout: float,
) -> Tuple[bool, Any, str]:
    if ros_version == "1":
        command = ["rosservice", "call", service_name]
    else:
        command = [
            "ros2",
            "service",
            "call",
            service_name,
            SERVICE_TYPES[(ros_version, value_kind)],
            "{}",
        ]
    try:
        result = run_command(command, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, f"service call timed out after {timeout:.1f}s"
    if result.returncode != 0:
        return False, None, result.stdout.strip()
    return parse_service_data(result.stdout, value_kind)


def check_services(
    *,
    yaml_params: Dict[str, Any],
    ros_version: str,
    camera_name: str,
    env: Dict[str, str],
    timeout: float,
    emit: StatusLogger,
) -> List[Dict[str, Any]]:
    advertised = list_services(ros_version, env, timeout)
    rows = []
    for key, (service_suffix, value_kind) in sorted(SERVICE_CHECKS.items()):
        if key not in yaml_params:
            continue
        expected = yaml_params[key]
        service_name = f"/{camera_name}/{service_suffix}"
        row = {
            "name": key,
            "service": service_name,
            "expected": expected,
            "actual": None,
            "status": "pending",
            "message": "",
        }
        if is_placeholder_value(expected):
            row["status"] = "skipped"
            row["message"] = "placeholder value uses device default or auto selection"
            emit(f"[SERVICE][SKIPPED] {service_name}: placeholder value")
            rows.append(row)
            continue
        if service_name not in advertised:
            row["status"] = "unsupported"
            row["message"] = "service is not advertised"
            emit(f"[SERVICE][UNSUPPORTED] {service_name}: not advertised")
            rows.append(row)
            continue
        ok, actual, message = call_getter_service(
            ros_version=ros_version,
            service_name=service_name,
            value_kind=value_kind,
            env=env,
            timeout=timeout,
        )
        row["actual"] = actual
        if not ok:
            row["status"] = "failed"
            row["message"] = message
            emit(f"[SERVICE][FAIL] {service_name}: {message}")
        elif values_match(expected, actual):
            row["status"] = "passed"
            row["message"] = "matches"
            emit(f"[SERVICE][PASS] {service_name}: {actual!r}")
        else:
            row["status"] = "failed"
            row["message"] = f"expected {expected!r}, got {actual!r}"
            emit(f"[SERVICE][FAIL] {service_name}: {row['message']}")
        rows.append(row)
    return rows


def scan_launch_log_errors(log_path: Path, from_line: int) -> Tuple[List[str], int]:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return [], from_line
    new_lines = lines[from_line:]
    errors = [line.strip() for line in new_lines if _LAUNCH_ERROR_RE.search(line)]
    return errors, len(lines)


def wait_for_launch_start(
    session: LaunchSession,
    startup_timeout: float,
    *,
    camera_count: int = 1,
    emit: StatusLogger,
) -> None:
    ready_marker = "Initialize device cost:"
    deadline = time.monotonic() + startup_timeout
    emit(f"waiting for {camera_count} camera(s) to print '{ready_marker}' (timeout {startup_timeout:.1f}s)")
    log_line = 0
    while time.monotonic() < deadline:
        session.assert_running()
        error_lines, log_line = scan_launch_log_errors(session.log_path, log_line)
        if error_lines:
            raise RuntimeError(
                "launch log contains errors/warnings:\n"
                + "\n".join(f"  {line}" for line in error_lines[:10])
            )
        try:
            text = session.log_path.read_text(encoding="utf-8", errors="ignore")
            if text.count(ready_marker) >= camera_count:
                emit("all cameras startup confirmed")
                return
        except OSError:
            pass
        time.sleep(0.5)
    session.assert_running()
    raise RuntimeError(
        f"launch did not become ready within {startup_timeout:.1f}s "
        f"('{ready_marker}' never appeared for all {camera_count} camera(s))"
    )


def status_has_failure(rows: Iterable[Dict[str, Any]]) -> bool:
    return any(row.get("status") == "failed" for row in rows)


def format_table(rows: List[Dict[str, Any]], columns: List[str]) -> List[str]:
    if not rows:
        return ["No checks."]
    output = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        output.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return output


def _summary_camera_section(cam: Dict[str, Any]) -> List[str]:
    lines = [f"### Camera: {cam.get('camera', '')}"]
    if cam.get("error"):
        lines += ["", f"**Error:** {cam['error']}"]
        return lines
    lines += [
        "",
        "#### Parameter Checks",
        "",
        *format_table(cam.get("param_checks", []), ["name", "expected", "actual", "status", "message"]),
        "",
        "#### Topic Checks",
        "",
        *format_table(cam.get("topic_checks", []), ["name", "topic", "expected_enabled", "status", "message"]),
        "",
        "#### Service Checks",
        "",
        *format_table(cam.get("service_checks", []), ["name", "service", "expected", "actual", "status", "message"]),
    ]
    return lines


def build_summary(result: Dict[str, Any]) -> str:
    command = result.get("command", [])
    command_text = " ".join(shlex.quote(str(item)) for item in command) if command else ""
    repeat_total = result.get("repeat_total", 1)
    repeat_passed = result.get("repeat_passed", 0)
    lines = [
        "# Launch Param Load Stress",
        "",
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
        f"- ROS version: {result.get('ros_version', '')}",
        f"- Launch file: {result.get('launch_file', '')}",
        f"- Runs: {repeat_passed}/{repeat_total} passed",
        "",
    ]
    notes = result.get("notes", [])
    if notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in notes]
        lines.append("")
    for run in result.get("runs", []):
        run_idx = run.get("run", "?")
        run_status = run.get("status", "")
        lines.append(f"## Run {run_idx}/{repeat_total} — {run_status}")
        if run.get("error"):
            lines += ["", f"**Error:** {run['error']}", ""]
            continue
        lines.append("")
        for cam in run.get("cameras", []):
            lines += _summary_camera_section(cam)
            lines.append("")
    return "\n".join(lines) + "\n"


def _check_one_camera(
    *,
    camera_name: str,
    yaml_params: Dict[str, Any],
    supported_params: set,
    declaration_filter_enabled: bool,
    ros_version: str,
    runtime_env: Dict[str, str],
    topic_timeout: float,
    service_timeout: float,
    skip_topic_check: bool,
    skip_service_check: bool,
    save_images_count: int,
    jpg_quality: int,
    images_dir: Optional[Path],
    emit: StatusLogger,
) -> Dict[str, Any]:
    cam: Dict[str, Any] = {
        "camera": camera_name,
        "param_checks": [],
        "topic_checks": [],
        "service_checks": [],
        "saved_images": [],
    }
    cam["param_checks"] = check_params(
        yaml_params=yaml_params,
        supported_params=supported_params,
        declaration_filter_enabled=declaration_filter_enabled,
        ros_version=ros_version,
        camera_name=camera_name,
        env=runtime_env,
        timeout=service_timeout,
        emit=emit,
    )
    if skip_topic_check:
        pass
    else:
        cam["topic_checks"] = check_topics(
            yaml_params=yaml_params,
            ros_version=ros_version,
            camera_name=camera_name,
            env=runtime_env,
            timeout=topic_timeout,
            emit=emit,
        )
    if skip_service_check:
        pass
    else:
        cam["service_checks"] = check_services(
            yaml_params=yaml_params,
            ros_version=ros_version,
            camera_name=camera_name,
            env=runtime_env,
            timeout=service_timeout,
            emit=emit,
        )
    if save_images_count > 0 and images_dir is not None:
        cam["saved_images"] = save_topic_images(
            ros_version=ros_version,
            camera_name=camera_name,
            yaml_params=yaml_params,
            output_root=images_dir / sanitize_path_part(camera_name),
            save_images_count=save_images_count,
            jpg_quality=jpg_quality,
            timeout=topic_timeout,
            emit=emit,
        )
    return cam


def _build_camera_launch_args(
    common_launch_args: Dict[str, Any],
    camera: "CameraSpec",
    shared_config_path: Optional[Path],
) -> Dict[str, Any]:
    launch_args = dict(common_launch_args)
    launch_args["camera_name"] = camera.name
    cfg = camera.config_file_path or (str(shared_config_path) if shared_config_path else "")
    if cfg:
        launch_args["config_file_path"] = cfg
    if camera.serial_number:
        launch_args["serial_number"] = camera.serial_number
    if camera.usb_port:
        launch_args["usb_port"] = camera.usb_port
    return launch_args


def run(args) -> int:
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    runtime_env = prepare_runtime_env(args)

    if args.camera:
        cameras = [parse_camera_spec(raw) for raw in args.camera]
        if len(cameras) > 1 and any(not c.serial_number and not c.usb_port for c in cameras):
            raise ValueError("multi-camera mode requires serial_number or usb_port for every --camera")
    else:
        cameras = [CameraSpec(
            name=args.camera_name,
            serial_number=getattr(args, "serial_number", ""),
            usb_port=getattr(args, "usb_port", ""),
            config_file_path="",
        )]

    shared_config_path = Path(args.config_file_path).expanduser().resolve() if args.config_file_path else None

    # Load yaml_params per camera (per-camera config overrides shared config)
    camera_configs: List[Path] = []
    for camera in cameras:
        if camera.config_file_path:
            camera_configs.append(Path(camera.config_file_path).expanduser().resolve())
        elif shared_config_path:
            camera_configs.append(shared_config_path)
        else:
            raise ValueError(f"no config_file_path for camera '{camera.name}'")
    yaml_params_list = [load_yaml_file(p) for p in camera_configs]

    # Common launch args (everything except per-camera overrides)
    common_launch_args: Dict[str, Any] = {}
    for raw_arg in args.launch_arg:
        key, value = parse_launch_arg(raw_arg)
        common_launch_args[key] = value

    launch_path = resolve_launch_file_path(
        ros_version=args.ros_version,
        launch_package=args.launch_package,
        launch_file=args.launch_file,
        env=runtime_env,
    )
    supported_params, declaration_note = discover_supported_params(launch_path, args.ros_version)
    declaration_filter_enabled = bool(supported_params)

    startup_timeout = parse_duration(args.startup_timeout, 30.0)
    topic_timeout = parse_duration(args.topic_timeout, 20.0)
    service_timeout = parse_duration(args.service_timeout, 15.0)
    repeat_total = args.repeat
    save_images_count = args.save_images_count
    jpg_quality = args.jpg_quality

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_launch_param_load_stress")
    default_results_dir = Path(__file__).resolve().parent / "results" / run_id
    results_dir = ensure_dir(Path(args.results_dir).resolve() if args.results_dir else default_results_dir)
    emit = StatusLogger()

    result: Dict[str, Any] = {
        "status": "failed",
        "tool_version": TOOL_VERSION,
        "ros_version": args.ros_version,
        "cameras": [asdict(c) for c in cameras],
        "launch_package": args.launch_package,
        "launch_file": args.launch_file,
        "notes": [declaration_note],
        "repeat_total": repeat_total,
        "repeat_passed": 0,
        "runs": [],
    }

    emit(f"tool version: {TOOL_VERSION}")
    emit(f"results dir: {results_dir}")
    emit(f"cameras: {', '.join(c.name for c in cameras)}")
    emit(f"repeat: {repeat_total}")

    try:
        for repeat_idx in range(repeat_total):
            if INTERRUPTED:
                break
            emit(f"--- run {repeat_idx + 1}/{repeat_total} ---")
            run_dir = ensure_dir(results_dir / f"run{repeat_idx + 1:03d}")
            run_result: Dict[str, Any] = {"run": repeat_idx + 1, "status": "failed", "cameras": []}
            sessions: List[LaunchSession] = []
            try:
                # Start all camera launches
                for camera in cameras:
                    launch_args = _build_camera_launch_args(common_launch_args, camera, shared_config_path)
                    command = build_launch_command(
                        ros_version=args.ros_version,
                        launch_package=args.launch_package,
                        launch_file=args.launch_file,
                        launch_args=launch_args,
                    )
                    log_path = run_dir / f"{camera.name}.log"
                    session = LaunchSession(
                        command=command,
                        work_dir=run_dir,
                        env=runtime_env,
                        log_path=log_path,
                        emit=emit,
                    )
                    emit(f"launch {camera.name}: " + " ".join(shlex.quote(a) for a in command))
                    session.start()
                    sessions.append(session)

                # Wait for each camera to be ready
                for camera, session in zip(cameras, sessions):
                    emit(f"waiting for {camera.name} to start...")
                    wait_for_launch_start(session, startup_timeout, emit=emit)

                # Check each camera
                run_label = f"run{repeat_idx + 1:03d}"
                images_dir = results_dir / "images" / run_label if save_images_count > 0 else None
                for camera, session, yaml_params in zip(cameras, sessions, yaml_params_list):
                    emit(f"checking camera: {camera.name}")
                    cam = _check_one_camera(
                        camera_name=camera.name,
                        yaml_params=yaml_params,
                        supported_params=supported_params,
                        declaration_filter_enabled=declaration_filter_enabled,
                        ros_version=args.ros_version,
                        runtime_env=runtime_env,
                        topic_timeout=topic_timeout,
                        service_timeout=service_timeout,
                        skip_topic_check=args.skip_topic_check,
                        skip_service_check=args.skip_service_check,
                        save_images_count=save_images_count,
                        jpg_quality=jpg_quality,
                        images_dir=images_dir,
                        emit=emit,
                    )
                    run_result["cameras"].append(cam)

                failed = any(
                    status_has_failure(cam[k])
                    for cam in run_result["cameras"]
                    for k in ("param_checks", "topic_checks", "service_checks")
                )
                run_result["status"] = "failed" if failed else "passed"
            except KeyboardInterrupt:
                run_result["status"] = "interrupted"
                emit("run interrupted by user")
            except Exception as exc:  # noqa: BLE001
                run_result["status"] = "failed"
                run_result["error"] = str(exc)
                emit(f"run failed: {exc}")
            finally:
                for session in sessions:
                    session.stop()

            result["runs"].append(run_result)
            if run_result["status"] == "passed":
                result["repeat_passed"] += 1
            emit(f"run {repeat_idx + 1}/{repeat_total}: {run_result['status']}")
            if INTERRUPTED:
                break

        if INTERRUPTED:
            result["status"] = "interrupted"
        else:
            result["status"] = "passed" if result["repeat_passed"] == repeat_total else "failed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        emit("test interrupted by user")
    finally:
        (results_dir / "summary.md").write_text(build_summary(result), encoding="utf-8")
        (results_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        signal.signal(signal.SIGINT, previous_sigint_handler)

    passed = result["repeat_passed"]
    emit(f"finished: {passed}/{repeat_total} runs passed")
    if result["status"] == "passed":
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Stress-test Orbbec camera launch parameter loading via config_file_path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Single camera\n"
            "  python3 ./launch_param_load_stress.py --ros-version 2 "
            "--launch-file gemini_330_series.launch.py "
            "--config-file-path /path/to/test_config.yaml\n\n"
            "  # Multi-camera (each with own serial and config)\n"
            "  python3 ./launch_param_load_stress.py --ros-version 2 "
            "--launch-file gemini_330_series.launch.py "
            "--camera camera1,serial_number=SN001,config_file_path=/path/cam1.yaml "
            "--camera camera2,serial_number=SN002,config_file_path=/path/cam2.yaml\n\n"
            "  # Stress test (repeat 20 times)\n"
            "  python3 ./launch_param_load_stress.py --ros-version 2 "
            "--launch-file gemini_330_series.launch.py "
            "--config-file-path /path/to/test_config.yaml --repeat 20\n"
        ),
    )
    parser.add_argument(
        "--show-verification-map",
        action="store_true",
        help="Print what each verification layer checks and exit without launching",
    )
    parser.add_argument("--ros-version", choices=("1", "2"), default=os.environ.get("ROS_VERSION", "2"))
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument(
        "--driver-setup",
        default=os.environ.get("ORBBEC_DRIVER_SETUP", os.environ.get("ORBBEC_CAMERA_SETUP", "")),
    )
    parser.add_argument("--launch-package", default="orbbec_camera")
    parser.add_argument("--launch-file", default="", help="Launch filename or absolute/relative launch path")
    # Multi-camera: --camera name[,serial_number=SN][,usb_port=XX][,config_file_path=/path]
    parser.add_argument("--camera", action="append", default=[],
                        metavar="SPEC",
                        help="Camera spec: name[,serial_number=SN][,usb_port=XX][,config_file_path=/path] "
                             "(repeatable for multi-camera)")
    # Single-camera convenience args (used when --camera is not specified)
    parser.add_argument("--camera-name", default="camera", help="Camera name (single-camera mode)")
    parser.add_argument("--serial-number", default="", help="Serial number (single-camera mode)")
    parser.add_argument("--usb-port", default="", help="USB port (single-camera mode)")
    parser.add_argument("--config-file-path", default="",
                        help="Shared config YAML (or per-camera via --camera config_file_path=)")
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra launch arg, KEY=VALUE or KEY:=VALUE")
    parser.add_argument("--startup-timeout", default="30", help="Wait time before checks, supports seconds, 1m")
    parser.add_argument("--topic-timeout", default="20", help="Max wait time for each enabled stream topic")
    parser.add_argument("--service-timeout", default="15", help="Max wait time for each param/service command")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="Repeat the full test cycle N times (stress test)")
    parser.add_argument("--results-dir", default="")
    parser.add_argument("--skip-topic-check", action="store_true")
    parser.add_argument("--skip-service-check", action="store_true")
    parser.add_argument(
        "--save-images-count",
        type=int,
        default=1,
        metavar="N",
        help="Save N images per enabled stream topic per camera (0 = disabled)",
    )
    parser.add_argument(
        "--jpg-quality",
        type=int,
        default=80,
        metavar="Q",
        help="JPEG compression quality 1-100 for saved images (default: 80)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(TOOL_VERSION),
    )
    args = parser.parse_args()
    if not args.show_verification_map:
        if not args.launch_file:
            parser.error("--launch-file is required unless --show-verification-map is used")
        if not args.camera and not args.config_file_path:
            parser.error("--config-file-path is required when --camera is not used")
        if args.repeat < 1:
            parser.error("--repeat must be >= 1")
    return args


def main() -> None:
    try:
        args = parse_args()
        if args.show_verification_map:
            print(build_verification_map_text(), end="")
            sys.exit(0)
        sys.exit(run(args))
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
