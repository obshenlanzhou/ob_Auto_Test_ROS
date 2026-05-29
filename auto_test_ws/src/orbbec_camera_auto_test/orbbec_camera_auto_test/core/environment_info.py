from __future__ import annotations

import json
import os
import platform
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import psutil

from .ros_utils import resolve_service_type
from .session import default_ros_setup


def _bash_setup_variant(setup_file: str) -> str:
    path = Path(setup_file)
    if path.name == "setup.zsh":
        candidate = path.with_name("setup.bash")
        if candidate.is_file():
            return str(candidate)
    return setup_file


def _read_os_pretty_name() -> str:
    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        return platform.platform()

    try:
        for line in os_release.read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        return platform.platform()
    return platform.platform()


def _read_cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        try:
            for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                normalized_key = key.strip().lower()
                if normalized_key in {"model name", "hardware", "processor"}:
                    model = value.strip()
                    if model:
                        return model
        except OSError:
            pass

    return platform.processor() or platform.machine()


def _collect_ros_environment(
    driver_setup: Optional[str] = None,
    ros_version: str = "2",
    ros_setup: Optional[str] = None,
) -> Dict[str, str]:
    setup_file = _bash_setup_variant(ros_setup or default_ros_setup(ros_version))
    command_parts = [f"source {shlex.quote(setup_file)} >/dev/null 2>&1"]
    if driver_setup:
        driver_setup = _bash_setup_variant(driver_setup)
        command_parts.append(f"source {shlex.quote(driver_setup)} >/dev/null 2>&1")
    command = " && ".join(command_parts) + " && printf '%s\\n%s\\n' \"$ROS_DISTRO\" \"$ROS_VERSION\""

    try:
        output = subprocess.check_output(
            ["bash", "-lc", command],
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        ).splitlines()
    except Exception:  # noqa: BLE001
        return {"ros_distro": "", "ros_version": ""}

    ros_distro = output[0].strip() if len(output) > 0 else ""
    ros_version = output[1].strip() if len(output) > 1 else ""
    return {"ros_distro": ros_distro, "ros_version": ros_version}


def collect_host_environment(
    driver_setup: Optional[str] = None,
    ros_version: str = "2",
    ros_setup: Optional[str] = None,
) -> Dict[str, Any]:
    memory = psutil.virtual_memory()
    host_environment: Dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": _read_os_pretty_name(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": _read_cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False) or 0,
        "logical_cpus": psutil.cpu_count(logical=True) or 0,
        "total_memory_gb": round(memory.total / (1024.0 ** 3), 2),
        "python_version": platform.python_version(),
    }
    host_environment.update(_collect_ros_environment(driver_setup, ros_version, ros_setup))
    host_environment["requested_ros_version"] = str(ros_version)
    if ros_setup:
        host_environment["ros_setup"] = str(ros_setup)
    if driver_setup:
        host_environment["driver_setup"] = str(driver_setup)
    return host_environment


def collect_camera_environment(
    harness,
    camera_name: str,
    launch_file: str,
    launch_args: Dict[str, Any],
) -> Dict[str, Any]:
    camera_environment: Dict[str, Any] = {
        "camera_name": camera_name,
        "launch_file": launch_file,
        "launch_args": dict(launch_args),
    }

    try:
        sdk_response = harness.call_service(
            f"/{camera_name}/get_sdk_version",
            resolve_service_type("orbbec_camera_msgs/srv/GetString", harness.ros_version),
            request_data={},
            timeout=10.0,
        )
        if getattr(sdk_response, "success", False):
            sdk_version = str(getattr(sdk_response, "data", "")).strip()
            if sdk_version:
                camera_environment["sdk_version"] = sdk_version
                try:
                    parsed_sdk = json.loads(sdk_version)
                except json.JSONDecodeError:
                    parsed_sdk = None
                if isinstance(parsed_sdk, dict):
                    if parsed_sdk.get("ob_sdk_version"):
                        camera_environment["ob_sdk_version"] = parsed_sdk["ob_sdk_version"]
                    if parsed_sdk.get("ros_sdk_version"):
                        camera_environment["ros_sdk_version"] = parsed_sdk["ros_sdk_version"]
                    if (
                        "firmware_version" not in camera_environment
                        and parsed_sdk.get("firmware_version")
                    ):
                        camera_environment["firmware_version"] = parsed_sdk["firmware_version"]
                    if (
                        "supported_min_sdk_version" not in camera_environment
                        and parsed_sdk.get("supported_min_sdk_version")
                    ):
                        camera_environment["supported_min_sdk_version"] = parsed_sdk[
                            "supported_min_sdk_version"
                        ]
    except Exception:  # noqa: BLE001
        pass

    try:
        device_response = harness.call_service(
            f"/{camera_name}/get_device_info",
            resolve_service_type("orbbec_camera_msgs/srv/GetDeviceInfo", harness.ros_version),
            request_data={},
            timeout=10.0,
        )
        if getattr(device_response, "success", False):
            info = getattr(device_response, "info", None)
            if info is not None:
                if getattr(info, "name", ""):
                    camera_environment["camera_model"] = info.name
                if getattr(info, "serial_number", ""):
                    camera_environment["serial_number"] = info.serial_number
                if getattr(info, "firmware_version", ""):
                    camera_environment["firmware_version"] = info.firmware_version
                if getattr(info, "hardware_version", ""):
                    camera_environment["hardware_version"] = info.hardware_version
                if getattr(info, "supported_min_sdk_version", ""):
                    camera_environment["supported_min_sdk_version"] = (
                        info.supported_min_sdk_version
                    )
                if "sdk_version" not in camera_environment and getattr(
                    info, "current_sdk_version", ""
                ):
                    camera_environment["sdk_version"] = info.current_sdk_version
    except Exception:  # noqa: BLE001
        pass

    return camera_environment
