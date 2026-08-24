from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


RESULT_SCHEMA_VERSION = 1
RESULT_STATUSES = {"passed", "failed", "interrupted"}
CAMERA_FIELDS = {
    "name": "name",
    "serial-number": "serial_number",
    "usb-port": "usb_port",
    "device-ip": "device_ip",
    "device-port": "device_port",
    "config-file-path": "config_file_path",
}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEVICE_FIELDS = {
    "name": "camera_model",
    "pid": "pid",
    "serial": "serial_number",
    "connection": "connection",
    "firmware version": "firmware_version",
    "usb port": "usb_port",
    "preset version": "preset_version",
}


class _TerminalTee:
    def __init__(self, primary, log_stream, lock: threading.Lock) -> None:
        self.primary = primary
        self.log_stream = log_stream
        self.lock = lock

    def write(self, text: str) -> int:
        with self.lock:
            self.primary.write(text)
            self.log_stream.write(text)
            self.log_stream.flush()
        return len(text)

    def flush(self) -> None:
        with self.lock:
            self.primary.flush()
            self.log_stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)


_TERMINAL_LOG_HANDLE = None
_TERMINAL_STDOUT_TEE = None
_TERMINAL_STDERR_TEE = None


def close_terminal_log() -> None:
    global _TERMINAL_LOG_HANDLE, _TERMINAL_STDOUT_TEE, _TERMINAL_STDERR_TEE
    if _TERMINAL_STDOUT_TEE is not None and sys.stdout is _TERMINAL_STDOUT_TEE:
        sys.stdout = _TERMINAL_STDOUT_TEE.primary
    if _TERMINAL_STDERR_TEE is not None and sys.stderr is _TERMINAL_STDERR_TEE:
        sys.stderr = _TERMINAL_STDERR_TEE.primary
    if _TERMINAL_LOG_HANDLE is not None:
        _TERMINAL_LOG_HANDLE.flush()
        _TERMINAL_LOG_HANDLE.close()
    _TERMINAL_LOG_HANDLE = None
    _TERMINAL_STDOUT_TEE = None
    _TERMINAL_STDERR_TEE = None


def install_terminal_log(path: Path) -> Path:
    global _TERMINAL_LOG_HANDLE, _TERMINAL_STDOUT_TEE, _TERMINAL_STDERR_TEE
    close_terminal_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = path.open("a", encoding="utf-8", buffering=1)
    lock = threading.Lock()
    stdout_tee = _TerminalTee(sys.stdout, log_stream, lock)
    stderr_tee = _TerminalTee(sys.stderr, log_stream, lock)
    _TERMINAL_LOG_HANDLE = log_stream
    _TERMINAL_STDOUT_TEE = stdout_tee
    _TERMINAL_STDERR_TEE = stderr_tee
    sys.stdout = stdout_tee
    sys.stderr = stderr_tee
    return path


def _request_value(request: Any, name: str, default: Any = "") -> Any:
    if isinstance(request, dict):
        return request.get(name, default)
    return getattr(request, name, default)


def _read_os_pretty_name() -> str:
    os_release = Path("/etc/os-release")
    if os_release.is_file():
        try:
            for line in os_release.read_text(encoding="utf-8").splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    return platform.platform()


def _read_cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    fallback = ""
    if cpuinfo.is_file():
        try:
            for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                normalized_key = key.strip().lower()
                if normalized_key in {"model name", "hardware"}:
                    model = value.strip()
                    if model:
                        return model
                elif normalized_key == "processor" and not fallback:
                    candidate = value.strip()
                    if candidate and not candidate.isdigit():
                        fallback = candidate
        except OSError:
            pass
    return fallback or platform.processor() or platform.machine()


def _total_memory_gb() -> float:
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return round((pages * page_size) / (1024.0 ** 3), 2)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0.0


def _camera_driver_version() -> str:
    prefixes = []
    for variable in ("AMENT_PREFIX_PATH", "CMAKE_PREFIX_PATH", "ROS_PACKAGE_PATH"):
        prefixes.extend(
            value for value in os.environ.get(variable, "").split(os.pathsep) if value
        )
    for prefix in prefixes:
        root = Path(prefix)
        candidates = (
            root / "share" / "orbbec_camera" / "package.xml",
            root / "orbbec_camera" / "package.xml",
            root / "package.xml",
        )
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                package = ET.parse(candidate).getroot()
                if package.findtext("name", "").strip() != "orbbec_camera":
                    continue
                version = package.findtext("version", "").strip()
                if version:
                    return version
            except (OSError, ET.ParseError):
                continue
    return ""


def _ob_sdk_version() -> str:
    try:
        library = ctypes.CDLL("libOrbbecSDK.so")
        for name in ("ob_get_major_version", "ob_get_minor_version", "ob_get_patch_version"):
            getattr(library, name).restype = ctypes.c_int
        return ".".join(
            str(getattr(library, name)())
            for name in (
                "ob_get_major_version",
                "ob_get_minor_version",
                "ob_get_patch_version",
            )
        )
    except (AttributeError, OSError):
        return ""


def _device_log_message(line: str) -> str:
    cleaned = _ANSI_ESCAPE.sub("", line).strip()
    if "]: " in cleaned:
        return cleaned.rsplit("]: ", 1)[1].strip()
    return cleaned


def _parse_camera_devices(output: str) -> list[Dict[str, Any]]:
    devices: list[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in output.splitlines():
        message = _device_log_message(raw_line)
        match = re.match(r"^([A-Za-z][A-Za-z _-]*):\s*(.*)$", message)
        if not match:
            continue
        raw_key, value = match.groups()
        key = raw_key.strip().lower().replace("_", " ")
        if key == "name":
            current = {"camera_model": value.strip()}
            devices.append(current)
            continue
        if current is None:
            continue
        field_name = _DEVICE_FIELDS.get(key)
        if field_name:
            current[field_name] = value.strip()
    return devices


def _collect_camera_devices(ros_version: str) -> list[Dict[str, Any]]:
    command = (
        ["rosrun", "orbbec_camera", "list_devices_node"]
        if str(ros_version) == "1"
        else ["ros2", "run", "orbbec_camera", "list_devices_node"]
    )
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
            env={**os.environ, "RCUTILS_COLORIZED_OUTPUT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return _parse_camera_devices(completed.stdout or "")


def collect_test_environment(
    request: Any = None,
    *,
    include_camera_devices: bool = True,
) -> Dict[str, Any]:
    requested_ros_version = str(
        _request_value(request, "ros_version", os.environ.get("ROS_VERSION", ""))
        or ""
    )
    host: Dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": _read_os_pretty_name(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": _read_cpu_model(),
        "logical_cpus": os.cpu_count() or 0,
        "total_memory_gb": _total_memory_gb(),
        "python_version": platform.python_version(),
        "ros_distro": os.environ.get("ROS_DISTRO", ""),
        "ros_version": os.environ.get("ROS_VERSION", "") or requested_ros_version,
        "requested_ros_version": requested_ros_version,
        "ob_sdk_version": _ob_sdk_version(),
        "camera_driver_version": _camera_driver_version(),
    }
    ros_setup = str(_request_value(request, "ros_setup", "") or "").strip()
    driver_setup = str(_request_value(request, "driver_setup", "") or "").strip()
    if ros_setup:
        host["ros_setup"] = ros_setup
    if driver_setup:
        host["driver_setup"] = driver_setup
    environment: Dict[str, Any] = {"host": host}
    if include_camera_devices:
        environment["cameras"] = _collect_camera_devices(requested_ros_version)
    return environment


def test_environment_markdown(environment: Dict[str, Any]) -> list[str]:
    host = environment.get("host", {}) if isinstance(environment, dict) else {}
    fields = (
        ("Hostname", "hostname"),
        ("OS", "os"),
        ("Kernel", "kernel"),
        ("Architecture", "architecture"),
        ("CPU model", "cpu_model"),
        ("Logical CPUs", "logical_cpus"),
        ("Total memory (GB)", "total_memory_gb"),
        ("Python version", "python_version"),
        ("ROS distro", "ros_distro"),
        ("ROS version", "ros_version"),
        ("OB SDK version", "ob_sdk_version"),
        ("Camera driver version", "camera_driver_version"),
    )
    lines = ["## Test Environment", ""]
    populated = False
    for label, key in fields:
        value = host.get(key, "")
        if value in ("", None):
            continue
        escaped = str(value).replace("`", "\\`")
        lines.append(f"- {label}: `{escaped}`")
        populated = True
    if not populated:
        lines.append("- Environment information unavailable")
    cameras = environment.get("cameras", []) if isinstance(environment, dict) else []
    if cameras:
        lines.extend(
            [
                f"- Detected cameras: `{len(cameras)}`",
                "",
                "| Camera | Serial Number | Firmware | Connection | USB Port |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for camera in cameras:
            values = [
                camera.get("camera_model", ""),
                camera.get("serial_number", ""),
                camera.get("firmware_version", ""),
                camera.get("connection", ""),
                camera.get("usb_port", ""),
            ]
            escaped_values = [
                str(value).replace("|", "\\|").replace("\n", " ") for value in values
            ]
            lines.append("| " + " | ".join(escaped_values) + " |")
    lines.append("")
    return lines


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_camera(raw: str, default_name: str = "camera") -> Dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("--camera cannot be empty")
    values = {value: "" for value in CAMERA_FIELDS.values()}
    for part in (item.strip() for item in text.split(",")):
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"camera field must be KEY=VALUE: {part}")
        key, value = (item.strip() for item in part.split("=", 1))
        target = CAMERA_FIELDS.get(key)
        if target is None:
            raise ValueError(f"unsupported --camera field: {key}")
        values[target] = value
    values["name"] = values["name"] or default_name
    return values


def json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def namespace_request(args: Any, *, exclude: Iterable[str] = ()) -> Dict[str, Any]:
    ignored = set(exclude)
    return {
        key: json_ready(value)
        for key, value in vars(args).items()
        if key not in ignored
    }


def invocation_record() -> Dict[str, Any]:
    argv = [sys.executable or "python3", *sys.argv]
    return {
        "command": shlex.join(argv),
        "argv": argv,
        "cwd": str(Path.cwd()),
    }


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class EventWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, message: str = "", **fields: Any) -> None:
        payload = {
            "time": iso_now(),
            "event": event,
            **json_ready(fields),
        }
        if message:
            payload["message"] = message
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")


def artifact_list(results_dir: Path) -> list[Dict[str, Any]]:
    artifacts = []
    for path in sorted(results_dir.rglob("*")):
        if not path.is_file() or path.name == "result.json":
            continue
        artifacts.append(
            {
                "path": str(path.relative_to(results_dir)),
                "size_bytes": path.stat().st_size,
            }
        )
    return artifacts


def contract_result(
    *,
    test_id: str,
    run_id: str,
    started_at: str,
    ended_at: str,
    request: Dict[str, Any],
    details: Dict[str, Any],
    environment: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
    artifacts: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    status = str(details.get("status") or "failed")
    if status not in RESULT_STATUSES:
        status = "failed"
    warnings = details.get("warnings")
    resolved_environment = environment or details.get("environment")
    if not isinstance(resolved_environment, dict):
        resolved_environment = collect_test_environment(
            request,
            include_camera_devices=False,
        )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "test_id": test_id,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": float(
            details.get("elapsed_seconds", details.get("duration_seconds", 0.0)) or 0.0
        ),
        "invocation": invocation_record(),
        "request": json_ready(request),
        "environment": json_ready(resolved_environment),
        "summary": json_ready(summary or {}),
        "warnings": json_ready(warnings if isinstance(warnings, list) else []),
        "details": json_ready(details),
        "artifacts": json_ready(artifacts or []),
        "error": details.get("error"),
    }
