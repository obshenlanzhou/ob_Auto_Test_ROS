from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from .run_manager import setup_defaults


DEVICE_COMMAND = ["ros2", "run", "orbbec_camera", "list_devices_node"]
DEVICE_QUERY_TIMEOUT_SECONDS = 20
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FIELD_NAMES = {
    "name": "name",
    "pid": "pid",
    "serial": "serial",
    "connection": "connection",
    "firmware version": "firmware_version",
    "usb port": "usb_port",
    "preset version": "preset_version",
}


class DeviceQueryError(RuntimeError):
    def __init__(self, message: str, *, status: int, output: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.output = output


def _message_from_log_line(line: str) -> str:
    cleaned = _ANSI_ESCAPE.sub("", line).strip()
    if "]: " in cleaned:
        return cleaned.rsplit("]: ", 1)[1].strip()
    return cleaned


def parse_device_output(output: str) -> List[Dict[str, Any]]:
    """Parse list_devices_node log output into one dictionary per camera."""
    devices: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    active_list = ""

    for raw_line in output.splitlines():
        message = _message_from_log_line(raw_line)
        if not message:
            continue
        if message.startswith("- "):
            if current is not None and active_list:
                current[active_list].append(message[2:].strip())
            continue

        match = re.match(r"^([A-Za-z][A-Za-z _-]*):\s*(.*)$", message)
        if not match:
            active_list = ""
            continue
        raw_key, value = match.groups()
        key = raw_key.strip().lower().replace("_", " ")

        if key == "name":
            current = {
                "name": value.strip(),
                "pid": "",
                "serial": "",
                "connection": "",
                "firmware_version": "",
                "usb_port": "",
                "device_presets": [],
                "color_presets": [],
                "preset_version": "",
            }
            devices.append(current)
            active_list = ""
            continue
        if current is None:
            continue

        if key == "device preset count":
            active_list = "device_presets"
        elif key == "color preset count":
            active_list = "color_presets"
        else:
            active_list = ""
            field_name = _FIELD_NAMES.get(key)
            if field_name:
                current[field_name] = value.strip()

    return devices


def _setup_path(value: Any, *, label: str, required: bool) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise DeviceQueryError(f"{label} is required", status=400)
        return ""
    path = Path(text).expanduser()
    if not path.is_file():
        raise DeviceQueryError(f"{label} file not found: {path}", status=400)
    return str(path)


def query_camera_devices(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise DeviceQueryError("request body must be a JSON object", status=400)
    ros_version = str(payload.get("ros_version") or "2").strip()
    if ros_version != "2":
        raise DeviceQueryError("camera information is only available for ROS 2", status=400)

    defaults = setup_defaults()["2"]
    ros_setup = _setup_path(
        payload.get("ros_setup") or defaults["ros_setup"],
        label="ROS setup",
        required=True,
    )
    camera_setup = _setup_path(
        payload.get("camera_setup") or defaults["driver_setup"],
        label="camera ROS setup",
        required=False,
    )
    shell = "zsh" if ros_setup.endswith(".zsh") or camera_setup.endswith(".zsh") else "bash"
    if shutil.which(shell) is None:
        raise DeviceQueryError(f"{shell} was not found in PATH", status=400)

    commands = ["set -e", "unset ROS_MASTER_URI ROS_ROOT ROS_PACKAGE_PATH"]
    commands.append(f"source {shlex.quote(ros_setup)}")
    if camera_setup:
        commands.append(f"source {shlex.quote(camera_setup)}")
    commands.append(" ".join(shlex.quote(item) for item in DEVICE_COMMAND))
    script = "\n".join(commands)
    env = {**os.environ, "RCUTILS_COLORIZED_OUTPUT": "0"}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [shell, "-lc", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=DEVICE_QUERY_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise DeviceQueryError(
            f"camera query timed out after {DEVICE_QUERY_TIMEOUT_SECONDS} seconds",
            status=504,
            output=output,
        ) from exc

    output = completed.stdout or ""
    if completed.returncode != 0:
        raise DeviceQueryError(
            f"list_devices_node exited with code {completed.returncode}",
            status=502,
            output=output,
        )
    devices = parse_device_output(output)
    return {
        "devices": devices,
        "count": len(devices),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output": output,
    }
