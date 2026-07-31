from __future__ import annotations

import csv
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .standalone import (
    build_command as build_standalone_command,
    last_events,
    manifest_catalog,
    validate_request as validate_standalone_request,
    validate_result_contract,
)


DEFAULT_ROS_SETUP = "/opt/ros/humble/setup.bash"
DEFAULT_ROS1_SETUP = "/opt/ros/one/setup.bash"


def _first_existing_setup(base_dir: Path) -> str:
    for name in ("setup.bash", "setup.zsh"):
        candidate = base_dir / name
        if candidate.is_file():
            return str(candidate)
    return str(base_dir / "setup.bash")


def _setup_from_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_dir():
            return _first_existing_setup(path)
        return str(path)
    return ""


DEFAULT_CAMERA_SETUP = _setup_from_env("ORBBEC_ROS2_CAMERA_SETUP", "ORBBEC_DRIVER_SETUP")
DEFAULT_ROS1_CAMERA_SETUP = _setup_from_env("ORBBEC_ROS1_CAMERA_SETUP")
SPECIAL_LAUNCH_CONFIGS = {
    "gemini_301_series.launch.py": {"dual_color": "gemini305_dual_color.yaml"},
    "gemini_301_series.launch": {"dual_color": "gemini305_dual_color.yaml"},
    "gemini2L.launch.py": {"dual_ir": "gemini2L_dual_ir.yaml"},
    "gemini2L.launch": {"dual_ir": "gemini2L_dual_ir.yaml"},
}
STREAM_LAUNCH_ARGS = {
    "enable_color",
    "enable_depth",
    "enable_ir",
    "enable_left_ir",
    "enable_right_ir",
    "enable_point_cloud",
    "enable_colored_point_cloud",
    "enable_accel",
    "enable_gyro",
    "enable_sync_output_accel_gyro",
}


def _default_ros_setup_for_version(ros_version: str) -> str:
    return DEFAULT_ROS1_SETUP if str(ros_version) == "1" else DEFAULT_ROS_SETUP


def _default_camera_setup_for_version(ros_version: str) -> str:
    return DEFAULT_ROS1_CAMERA_SETUP if str(ros_version) == "1" else DEFAULT_CAMERA_SETUP


def _find_auto_test_ws() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "run_camera_auto_test.sh").is_file() and (parent / "src").is_dir():
            return parent

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "run_camera_auto_test.sh").is_file() and (parent / "src").is_dir():
            return parent

    return cwd


AUTO_TEST_WS = _find_auto_test_ws()
CORE_PACKAGE_ROOT = AUTO_TEST_WS / "src" / "orbbec_camera_auto_test"
RESULTS_ROOT = AUTO_TEST_WS / "results"
UI_RESULTS_ROOT = RESULTS_ROOT / "ui_runs"
CONFIG_PATH = RESULTS_ROOT / "ui_config.json"
STANDALONE_ROOT = AUTO_TEST_WS.parent / "standalone_test_scripts"
FRAMEWORK_MODES = {"functional", "performance", "restart", "stream_stall", "all"}
MODE_CONFIG_KEYS = (
    "performance_scenario",
    "launch_file",
    "launch_config",
    "stream_options",
    "run_count",
    "continue_on_error",
    "duration",
    "stable_seconds",
    "stream_timeout",
    "max_gap_seconds",
    "restart_delay",
    "image_topics",
    "warning_interval_sec",
    "warmup_sec",
    "save_csv",
    "queue_size",
    "camera_name",
    "serial_number",
    "usb_port",
    "config_file_path",
    "launch_args",
)


def _discover_sibling_driver_setup(ros_version: str) -> str:
    repositories_root = AUTO_TEST_WS.parent.parent
    if not repositories_root.is_dir():
        return ""
    if str(ros_version) == "1":
        marker = Path("src/OrbbecSDK_ROS1")
        setup = Path("devel/setup.bash")
    else:
        marker = Path("src/OrbbecSDK_ROS2")
        setup = Path("install/setup.bash")
    candidates = [
        repository / setup
        for repository in sorted(repositories_root.iterdir())
        if repository.is_dir()
        and (repository / marker).is_dir()
        and (repository / setup).is_file()
    ]
    preferred = [
        candidate
        for candidate in candidates
        if "v2" in candidate.parent.parent.name.lower()
    ]
    if len(preferred) == 1:
        return str(preferred[0])
    return str(candidates[0]) if len(candidates) == 1 else ""


DEFAULT_CAMERA_SETUP = DEFAULT_CAMERA_SETUP or _discover_sibling_driver_setup("2")
DEFAULT_ROS1_CAMERA_SETUP = DEFAULT_ROS1_CAMERA_SETUP or _discover_sibling_driver_setup("1")


def setup_defaults() -> Dict[str, Dict[str, str]]:
    return {
        "2": {
            "ros_setup": DEFAULT_ROS_SETUP,
            "driver_setup": DEFAULT_CAMERA_SETUP,
        },
        "1": {
            "ros_setup": DEFAULT_ROS1_SETUP,
            "driver_setup": DEFAULT_ROS1_CAMERA_SETUP,
        },
    }


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_unique_run_dir(base_run_id: str) -> tuple[str, Path]:
    ensure_dir(UI_RESULTS_ROOT)
    for index in range(1, 1000):
        run_id = base_run_id if index == 1 else f"{base_run_id}_{index:02d}"
        run_root = UI_RESULTS_ROOT / run_id
        try:
            run_root.mkdir()
        except FileExistsError:
            continue
        return run_id, run_root
    raise RuntimeError(f"unable to allocate results directory for {base_run_id}")


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_latest_csv_row(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as stream:
            header = stream.readline().decode("utf-8", errors="replace").strip()
            if not header:
                return {}
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            chunk_size = min(8192, end)
            stream.seek(max(0, end - chunk_size))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return {}
    if lines[0] == header and len(lines) == 1:
        return {}
    last_line = lines[-1]
    try:
        headers = next(csv.reader([header]))
        values = next(csv.reader([last_line]))
    except csv.Error:
        return {}
    return dict(zip(headers, values))


def _read_latest_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            header = stream.readline().decode("utf-8", errors="replace").strip()
            if not header:
                return []
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            chunk_size = min(32768, end)
            stream.seek(max(0, end - chunk_size))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return []
    if lines[0] == header:
        lines = lines[1:]
    try:
        headers = next(csv.reader([header]))
    except csv.Error:
        return []

    rows: List[Dict[str, str]] = []
    for line in lines:
        try:
            values = next(csv.reader([line]))
        except csv.Error:
            continue
        row = dict(zip(headers, values))
        if row:
            rows.append(row)
    if not rows:
        return []

    latest_elapsed = max(_float_value(row, "elapsed_seconds", -1.0) for row in rows)
    if latest_elapsed < 0.0:
        return [rows[-1]]
    return [
        row
        for row in rows
        if abs(_float_value(row, "elapsed_seconds", -1.0) - latest_elapsed) < 0.0005
    ]


def _latest_file(root: Path, name: str) -> Path | None:
    candidates = [path for path in root.rglob(name) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _float_value(payload: Dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(payload.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _wall_elapsed_seconds(started_at: str | None, ended_at: str | None) -> float:
    start = _parse_datetime(started_at)
    if start is None:
        return 0.0
    end = _parse_datetime(ended_at) or datetime.now()
    return max(0.0, (end - start).total_seconds())


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _build_standalone_progress(
    events: List[Dict[str, Any]],
    *,
    supported: bool,
    requested_total: Optional[int],
) -> Dict[str, Any]:
    if not supported:
        return {"supported": False, "current": None, "total": None}

    current = 0
    total = requested_total
    for event in reversed(events):
        if event.get("event") != "progress":
            continue
        event_current = _positive_int(event.get("current"))
        event_total = _positive_int(event.get("total"))
        if event_current is not None:
            current = event_current
        if event_total is not None:
            total = event_total
        break
    return {"supported": True, "current": current, "total": total}


def _duration_like_value(value: str) -> float:
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
    return float(raw) * multiplier


def _int_value(payload: Dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(payload.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _format_topic_label(label: str) -> str:
    if not label:
        return ""
    stream_markers = (
        ("left_color", "_left_color_"),
        ("right_color", "_right_color_"),
        ("left_ir", "_left_ir_"),
        ("right_ir", "_right_ir_"),
        ("color", "_color_"),
        ("depth", "_depth_"),
        ("ir", "_ir_"),
    )
    padded = f"_{label}_"
    for stream_name, marker in stream_markers:
        if marker not in padded:
            continue
        before, after = padded.split(marker, 1)
        camera_name = before.strip("_")
        suffix = after.strip("_").replace("_", "/").replace("image/raw", "image_raw")
        if camera_name:
            return f"/{camera_name}/{stream_name}/{suffix}".rstrip("/")
        return f"/{stream_name}/{suffix}".rstrip("/")
    return "/" + label.replace("_", "/").replace("image/raw", "image_raw")


_FRAME_CONFIG_RE = re.compile(
    r"\[(?P<node>[^\]]+)\]: (?P<stream>left_color|right_color|color|depth|left_ir|right_ir|ir) Frame - Width: "
    r"(?P<width>\d+) Height: (?P<height>\d+) fps: (?P<fps>\d+) Format: (?P<format>\S+)"
)


def _read_stream_config_from_launch_log(run_root: Path) -> Dict[tuple[str, str], Dict[str, Any]]:
    launch_path = _latest_file(run_root, "launch.log")
    if launch_path is None:
        return {}
    configs: Dict[tuple[str, str], Dict[str, Any]] = {}
    try:
        lines = launch_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        match = _FRAME_CONFIG_RE.search(line)
        if not match:
            continue
        node_name = match.group("node").split(".")[0]
        stream = match.group("stream")
        configs[(node_name, stream)] = {
            "width": int(match.group("width")),
            "height": int(match.group("height")),
            "fps": int(match.group("fps")),
            "format": match.group("format"),
        }
    return configs


def _stream_key_from_topic(topic_name: str) -> str:
    if "/left_color/" in topic_name:
        return "left_color"
    if "/right_color/" in topic_name:
        return "right_color"
    if "/left_ir/" in topic_name:
        return "left_ir"
    if "/right_ir/" in topic_name:
        return "right_ir"
    if "/color/" in topic_name:
        return "color"
    if "/depth/" in topic_name:
        return "depth"
    if "/ir/" in topic_name:
        return "ir"
    return ""


def _camera_name_from_topic(topic_name: str) -> str:
    parts = [part for part in topic_name.split("/") if part]
    if len(parts) >= 2:
        return parts[0]
    return ""


def _point_cloud_format(topic_name: str) -> str:
    normalized = topic_name.rstrip("/")
    if normalized.endswith("/depth_registered/points"):
        return "XYZRGB"
    if normalized.endswith("/points"):
        return "XYZ"
    return ""


def build_performance_metrics(
    run_root: Path,
    *,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> Dict[str, Any]:
    system_path = _latest_file(run_root, "system_usage.csv")
    fps_path = _latest_file(run_root, "fps.csv")
    system_rows = _read_latest_csv_rows(system_path) if system_path else []
    system_row = system_rows[-1] if system_rows else {}
    fps_row = _read_latest_csv_row(fps_path) if fps_path else {}
    preferred_system_row = next(
        (
            row
            for row in system_rows
            if row.get("scope") == "total" and row.get("camera_name") == "all"
        ),
        None,
    )
    if preferred_system_row is None:
        preferred_system_row = next(
            (row for row in system_rows if row.get("scope") == "shared_container"),
            None,
        )
    if preferred_system_row is None:
        preferred_system_row = system_row

    sampled_elapsed_seconds = max(
        _float_value(system_row, "elapsed_seconds"),
        _float_value(fps_row, "elapsed_seconds"),
    )
    elapsed_seconds = sampled_elapsed_seconds or _wall_elapsed_seconds(started_at, ended_at)
    stream_configs = _read_stream_config_from_launch_log(run_root)
    topics: List[Dict[str, Any]] = []
    suffixes = (
        "_ideal_fps",
        "_current_fps",
        "_avg_fps",
        "_dropped_frames",
        "_drop_rate",
    )
    labels = sorted(
        {
            key[: -len(suffix)]
            for key in fps_row
            for suffix in suffixes
            if key.endswith(suffix)
        }
    )
    for label in labels:
        topic_name = _format_topic_label(label)
        point_cloud_format = _point_cloud_format(topic_name)
        is_point_cloud = bool(point_cloud_format)
        stream_key = _stream_key_from_topic(topic_name)
        camera_name = _camera_name_from_topic(topic_name)
        stream_config = (
            stream_configs.get((camera_name, stream_key))
            or stream_configs.get(("", stream_key))
            or {}
        )
        topics.append(
            {
                "label": label,
                "topic": topic_name,
                "resolution": (
                    f"{stream_config.get('width')} x {stream_config.get('height')}"
                    if not is_point_cloud
                    and stream_config.get("width")
                    and stream_config.get("height")
                    else ""
                ),
                "stream_format": (
                    point_cloud_format if is_point_cloud else stream_config.get("format", "")
                ),
                "ideal_fps": _float_value(
                    {"value": stream_config.get("fps")},
                    "value",
                    _float_value(fps_row, f"{label}_ideal_fps"),
                ),
                "current_fps": _float_value(fps_row, f"{label}_current_fps"),
                "avg_fps": _float_value(fps_row, f"{label}_avg_fps"),
                "dropped_frames": _int_value(fps_row, f"{label}_dropped_frames"),
                "drop_rate": _float_value(fps_row, f"{label}_drop_rate"),
            }
        )

    system_scopes: List[Dict[str, Any]] = []
    for row in system_rows:
        scope = row.get("scope", "")
        camera_name = row.get("camera_name", "")
        if not scope and not camera_name:
            continue
        system_scopes.append(
            {
                "scope": scope,
                "camera_name": camera_name,
                "label": f"{scope}:{camera_name}" if scope and camera_name else camera_name or scope,
                "pid_count": _int_value(row, "pid_count"),
                "cpu_percent": _float_value(row, "cpu_percent"),
                "memory_rss_mb": _float_value(row, "memory_rss_mb"),
            }
        )

    return {
        "available": bool(system_row or fps_row),
        "elapsed_seconds": elapsed_seconds,
        "sampled_elapsed_seconds": sampled_elapsed_seconds,
        "pid_count": _int_value(preferred_system_row, "pid_count"),
        "cpu_percent": _float_value(preferred_system_row, "cpu_percent"),
        "memory_rss_mb": _float_value(preferred_system_row, "memory_rss_mb"),
        "system_scopes": system_scopes,
        "fps_topics": topics,
        "system_csv": str(system_path) if system_path else "",
        "fps_csv": str(fps_path) if fps_path else "",
    }


def build_restart_metrics(run_root: Path) -> Dict[str, Any]:
    result_path = _latest_file(run_root, "result.json")
    result = read_json(result_path, {}) if result_path else {}
    if isinstance(result, dict) and isinstance(result.get("details"), dict):
        result = result["details"]
    if not isinstance(result, dict) or "successful_restarts" not in result:
        return {"available": False}

    attempts = result.get("attempts", [])
    current_attempt = attempts[-1] if attempts else {}
    return {
        "available": True,
        "status": result.get("status", ""),
        "successful_restarts": int(result.get("successful_restarts", 0) or 0),
        "launch_attempts": int(result.get("launch_attempts", 0) or 0),
        "duration_seconds": float(result.get("duration_seconds", 0.0) or 0.0),
        "stable_seconds_required": float(result.get("stable_seconds_required", 0.0) or 0.0),
        "current_attempt": current_attempt.get("attempt", ""),
        "current_attempt_status": current_attempt.get("status", ""),
        "message": result.get("warning") or current_attempt.get("message", ""),
        "result_json": str(result_path) if result_path else "",
    }


def normalize_ros_domain_id(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d+", text):
        raise ValueError("ROS Domain ID must be an integer between 0 and 232")
    domain_id = int(text)
    if domain_id > 232:
        raise ValueError("ROS Domain ID must be an integer between 0 and 232")
    return str(domain_id)


def _ros_domain_environment_command(ros_version: Any, value: Any) -> str:
    if str(ros_version or "2").strip() != "2":
        return ""
    domain_id = normalize_ros_domain_id(value)
    return (
        f"export ROS_DOMAIN_ID={shlex.quote(domain_id)}"
        if domain_id
        else "unset ROS_DOMAIN_ID"
    )


def load_config() -> Dict[str, Any]:
    config = read_json(CONFIG_PATH, {})
    ros_version = str(config.get("ros_version") or "2")
    if ros_version not in {"1", "2"}:
        ros_version = "2"
    domain_value = config.get("ros_domain_id", "")
    try:
        ros_domain_id = normalize_ros_domain_id(domain_value)
    except ValueError:
        ros_domain_id = ""
    loaded = {
        "ros_version": ros_version,
        "ros_domain_id": ros_domain_id,
        "ros_setup": config.get("ros_setup") or _default_ros_setup_for_version(ros_version),
        "camera_setup": config.get("camera_setup") or _default_camera_setup_for_version(ros_version),
        "host": config.get("host") or "127.0.0.1",
        "port": int(config.get("port") or 8000),
        "mode": config.get("mode") or "functional",
        "performance_scenario": config.get("performance_scenario") or "",
        "launch_file": config.get("launch_file") or "",
        "launch_config": config.get("launch_config") or "generic",
        "stream_options": config.get("stream_options") or {},
        "run_count": config.get("run_count") or "1",
        "continue_on_error": config.get("continue_on_error", False),
        "duration": config.get("duration") or "",
        "stable_seconds": config.get("stable_seconds") or "10",
        "stream_timeout": config.get("stream_timeout") or "60",
        "max_gap_seconds": config.get("max_gap_seconds") or "1.5",
        "restart_delay": config.get("restart_delay") or "2",
        "image_topics": config.get("image_topics") or "",
        "warning_interval_sec": config.get("warning_interval_sec") or "1.0",
        "warmup_sec": config.get("warmup_sec") or "2.0",
        "save_csv": config.get("save_csv") or "true",
        "queue_size": config.get("queue_size") or "10",
        "camera_name": config.get("camera_name") or "",
        "serial_number": config.get("serial_number") or "",
        "usb_port": config.get("usb_port") or "",
        "config_file_path": config.get("config_file_path") or "",
        "launch_args": config.get("launch_args") or "",
        "standalone_configs": config.get("standalone_configs") or {},
    }
    raw_mode_configs = config.get("mode_configs")
    mode_configs = (
        {
            mode: {
                key: value
                for key, value in values.items()
                if key in MODE_CONFIG_KEYS
            }
            for mode, values in raw_mode_configs.items()
            if mode in FRAMEWORK_MODES and isinstance(values, dict)
        }
        if isinstance(raw_mode_configs, dict)
        else {}
    )
    active_mode = loaded["mode"]
    if active_mode not in mode_configs:
        # Migrate the previous single-history format without discarding it.
        mode_configs[active_mode] = {
            key: loaded[key] for key in MODE_CONFIG_KEYS
        }
    loaded["mode_configs"] = mode_configs
    return loaded


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config()
    for key in (
        "ros_setup",
        "ros_version",
        "ros_domain_id",
        "camera_setup",
        "host",
        "port",
        "mode",
        "performance_scenario",
        "launch_file",
        "launch_config",
        "stream_options",
        "run_count",
        "continue_on_error",
        "duration",
        "stable_seconds",
        "stream_timeout",
        "max_gap_seconds",
        "restart_delay",
        "image_topics",
        "warning_interval_sec",
        "warmup_sec",
        "save_csv",
        "queue_size",
        "camera_name",
        "serial_number",
        "usb_port",
        "config_file_path",
        "launch_args",
        "standalone_configs",
    ):
        if key in payload:
            config[key] = (
                normalize_ros_domain_id(payload[key])
                if key == "ros_domain_id"
                else payload[key]
            )
    mode = _safe_text(payload.get("mode") or config.get("mode")) or "functional"
    if mode in FRAMEWORK_MODES:
        mode_config = dict(config.get("mode_configs", {}).get(mode, {}))
        for key in MODE_CONFIG_KEYS:
            if key in payload:
                mode_config[key] = payload[key]
        config.setdefault("mode_configs", {})[mode] = mode_config
    write_json(CONFIG_PATH, config)
    return config


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _append_arg(args: List[str], name: str, value: Any) -> None:
    text = _safe_text(value)
    if text:
        args.extend([name, text])


def _parse_extra_launch_args(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [line.strip() for line in str(raw or "").splitlines() if line.strip()]


def _parse_multiline_values(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [line.strip() for line in str(raw or "").splitlines() if line.strip()]


def _parse_run_count(raw: Any) -> int:
    text = _safe_text(raw) or "1"
    return int(text)


def _bool_value(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return _safe_text(raw).lower() in {"1", "true", "yes", "on"}


def _selected_config_file(payload: Dict[str, Any]) -> str:
    launch_file = _safe_text(payload.get("launch_file"))
    launch_config = _safe_text(payload.get("launch_config")) or "generic"
    if launch_config == "generic":
        return _safe_text(payload.get("config_file_path"))
    return SPECIAL_LAUNCH_CONFIGS.get(launch_file, {}).get(launch_config, "")


def _stream_launch_args(payload: Dict[str, Any]) -> List[str]:
    if (_safe_text(payload.get("launch_config")) or "generic") != "generic":
        return []
    raw_options = payload.get("stream_options")
    if not isinstance(raw_options, dict):
        return []
    result = []
    for name in sorted(STREAM_LAUNCH_ARGS):
        value = _safe_text(raw_options.get(name)).lower()
        if value in {"true", "false"}:
            result.append(f"{name}={value}")
    return result


def _special_launch_args(payload: Dict[str, Any]) -> List[str]:
    launch_config = _safe_text(payload.get("launch_config"))
    if launch_config == "generic":
        return []
    config_file = _selected_config_file(payload)
    result = [f"config_file_path={config_file}"] if config_file else []
    if launch_config == "dual_color":
        result.append("device_preset=Dual Color Streams")
    return result


def _build_runner_args(payload: Dict[str, Any], mode: str, results_dir: Path) -> List[str]:
    modules = {
        "functional": "orbbec_camera_auto_test.runners.functional",
        "performance": "orbbec_camera_auto_test.runners.performance",
        "restart": "orbbec_camera_auto_test.runners.restart",
        "stream_stall": "orbbec_camera_auto_test.runners.stream_stall",
    }
    module = modules.get(mode, "orbbec_camera_auto_test.runners.performance")
    args = ["python3", "-m", module]
    ros_version = _safe_text(payload.get("ros_version")) or "2"
    args.extend(["--ros-version", ros_version])
    _append_arg(args, "--ros-setup", payload.get("ros_setup"))
    _append_arg(args, "--results-dir", str(results_dir))
    _append_arg(args, "--launch-file", payload.get("launch_file"))
    _append_arg(args, "--camera-name", payload.get("camera_name"))
    _append_arg(args, "--serial-number", payload.get("serial_number"))
    _append_arg(args, "--usb-port", payload.get("usb_port"))
    _append_arg(args, "--config-file-path", _selected_config_file(payload))
    _append_arg(args, "--driver-setup", payload.get("camera_setup"))

    if mode == "performance":
        _append_arg(args, "--scenario", payload.get("performance_scenario"))
    if mode == "performance":
        _append_arg(args, "--duration", payload.get("duration"))
    if mode == "restart":
        _append_arg(args, "--duration", payload.get("duration"))
        _append_arg(args, "--stable-seconds", payload.get("stable_seconds"))
        _append_arg(args, "--stream-timeout", payload.get("stream_timeout"))
        _append_arg(args, "--max-gap-seconds", payload.get("max_gap_seconds"))
        _append_arg(args, "--restart-delay", payload.get("restart_delay"))
        for topic in _parse_multiline_values(payload.get("image_topics")):
            args.extend(["--image-topic", topic])
    if mode == "stream_stall":
        _append_arg(args, "--duration", payload.get("duration"))
        _append_arg(args, "--warning-interval-sec", payload.get("warning_interval_sec"))
        _append_arg(args, "--warmup-sec", payload.get("warmup_sec"))
        _append_arg(args, "--save-csv", payload.get("save_csv"))
        _append_arg(args, "--queue-size", payload.get("queue_size"))
        for topic in _parse_multiline_values(payload.get("image_topics")):
            args.extend(["--image-topic", topic])

    launch_args = [
        *_stream_launch_args(payload),
        *_parse_extra_launch_args(payload.get("launch_args")),
        *_special_launch_args(payload),
    ]
    for launch_arg in launch_args:
        args.extend(["--launch-arg", launch_arg])
    return args


def _quote_command(args: List[str]) -> str:
    return " ".join(shlex.quote(item) for item in args)


def _append_runner_command(
    commands: List[str],
    displayed: List[str],
    args: List[str],
    *,
    continue_on_error: bool,
) -> None:
    command_line = _quote_command(args)
    displayed.append(command_line)
    commands.append(f"echo {shlex.quote(f'[UI] command: {command_line}')}")
    if not continue_on_error:
        commands.append(command_line)
        commands.append("echo '[UI] command done'")
        return
    commands.extend(
        [
            f"if {command_line}; then",
            "  echo '[UI] command done'",
            "else",
            "  UI_COMMAND_STATUS=$?",
            "  echo \"[UI] command failed with exit code ${UI_COMMAND_STATUS}; continuing\"",
            "  echo '[UI] command done'",
            "  if [ \"${UI_COMMAND_STATUS}\" -ge 128 ]; then exit \"${UI_COMMAND_STATUS}\"; fi",
            "  if [ \"${UI_EXIT_CODE}\" -eq 0 ]; then UI_EXIT_CODE=\"${UI_COMMAND_STATUS}\"; fi",
            "fi",
        ]
    )


def _matching_setup_variant(path: str, suffix: str) -> str:
    setup_path = Path(path)
    if setup_path.name not in {"setup.bash", "setup.zsh"}:
        return path
    candidate = setup_path.with_name(f"setup.{suffix}")
    return str(candidate) if candidate.is_file() else path


def _shell_for_setup(camera_setup: str, ros_setup: str = "") -> str:
    return "zsh" if camera_setup.endswith(".zsh") or ros_setup.endswith(".zsh") else "bash"


def _build_shell_script(payload: Dict[str, Any], run_root: Path) -> tuple[str, List[str], str]:
    mode = _safe_text(payload.get("mode")) or "functional"
    camera_setup = _safe_text(payload.get("camera_setup"))
    ros_version = _safe_text(payload.get("ros_version")) or "2"
    ros_setup = _safe_text(payload.get("ros_setup")) or _default_ros_setup_for_version(ros_version)
    shell = _shell_for_setup(camera_setup, ros_setup)
    if shell == "zsh":
        ros_setup = _matching_setup_variant(ros_setup, "zsh")
    commands = [
        "set -e",
    ]
    if ros_version == "1":
        commands.extend(
            [
                "unset ROS_DISTRO ROS_ETC_DIR AMENT_PREFIX_PATH COLCON_PREFIX_PATH",
                "export PATH=$(printf '%s' \"$PATH\" | tr ':' '\\n' | { grep -v '/opt/ros/humble' || true; } | paste -sd ':' -)",
                "export PYTHONPATH=$(printf '%s' \"${PYTHONPATH:-}\" | tr ':' '\\n' | { grep -v '/opt/ros/humble' || true; } | paste -sd ':' -)",
                "export LD_LIBRARY_PATH=$(printf '%s' \"${LD_LIBRARY_PATH:-}\" | tr ':' '\\n' | { grep -v '/opt/ros/humble' || true; } | paste -sd ':' -)",
            ]
        )
    else:
        commands.append("unset ROS_MASTER_URI ROS_ROOT ROS_PACKAGE_PATH")
    commands.append(f"source {shlex.quote(ros_setup)}")
    if camera_setup:
        commands.append(f"source {shlex.quote(camera_setup)}")
    domain_id = normalize_ros_domain_id(payload.get("ros_domain_id"))
    domain_command = _ros_domain_environment_command(ros_version, domain_id)
    if domain_command:
        commands.append(domain_command)
        domain_message = (
            f"[UI] ROS_DOMAIN_ID={domain_id}"
            if domain_id
            else "[UI] ROS_DOMAIN_ID is not set"
        )
        commands.append(f"echo {shlex.quote(domain_message)}")
    commands.append(f"export ORBBEC_ROS_VERSION={shlex.quote(ros_version)}")
    commands.append(f"export ORBBEC_ROS_SETUP={shlex.quote(ros_setup)}")
    commands.append(f"export PYTHONPATH={shlex.quote(str(CORE_PACKAGE_ROOT))}:\"${{PYTHONPATH:-}}\"")
    commands.append(f"cd {shlex.quote(str(AUTO_TEST_WS))}")

    displayed: List[str] = []
    run_count = _parse_run_count(payload.get("run_count"))
    continue_on_error = _bool_value(payload.get("continue_on_error"))
    if continue_on_error:
        commands.append("UI_EXIT_CODE=0")
    for iteration in range(1, run_count + 1):
        iteration_root = run_root / f"iteration_{iteration:02d}" if run_count > 1 else run_root
        if run_count > 1:
            commands.append(f"echo {shlex.quote(f'[UI] starting iteration {iteration}/{run_count}')}")

        if mode in ("functional", "all"):
            functional_dir = iteration_root / "functional" if mode == "all" else iteration_root
            args = _build_runner_args(payload, "functional", functional_dir)
            _append_runner_command(commands, displayed, args, continue_on_error=continue_on_error)
        if mode in ("performance", "all"):
            performance_dir = iteration_root / "performance" if mode == "all" else iteration_root
            args = _build_runner_args(payload, "performance", performance_dir)
            _append_runner_command(commands, displayed, args, continue_on_error=continue_on_error)
        if mode == "restart":
            args = _build_runner_args(payload, "restart", iteration_root)
            _append_runner_command(commands, displayed, args, continue_on_error=continue_on_error)
        if mode == "stream_stall":
            args = _build_runner_args(payload, "stream_stall", iteration_root)
            _append_runner_command(commands, displayed, args, continue_on_error=continue_on_error)
    if continue_on_error:
        commands.append("exit \"${UI_EXIT_CODE}\"")

    return "\n".join(commands), displayed, shell


def validate_run_payload(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    mode = _safe_text(payload.get("mode")) or "functional"
    if mode not in {"functional", "performance", "restart", "stream_stall", "all"}:
        errors.append(f"unsupported mode: {mode}")
    ros_version = _safe_text(payload.get("ros_version")) or "2"
    if ros_version not in {"1", "2"}:
        errors.append(f"unsupported ROS version: {ros_version}")
    try:
        normalize_ros_domain_id(payload.get("ros_domain_id"))
    except ValueError as exc:
        errors.append(str(exc))

    launch_file = _safe_text(payload.get("launch_file"))
    if not launch_file:
        errors.append("launch_file is required")
    launch_config = _safe_text(payload.get("launch_config")) or "generic"
    if launch_config != "generic" and not _selected_config_file(payload):
        errors.append(
            f"launch config '{launch_config}' is not supported by '{launch_file}'"
        )
    scenario = _safe_text(payload.get("performance_scenario"))
    if scenario and scenario not in {"default", "stress", "drop_frame"}:
        errors.append(f"unsupported performance scenario: {scenario}")
    stream_options = payload.get("stream_options")
    if stream_options is not None and not isinstance(stream_options, dict):
        errors.append("stream_options must be an object")
    elif isinstance(stream_options, dict):
        for name, value in stream_options.items():
            if name not in STREAM_LAUNCH_ARGS:
                errors.append(f"unsupported stream option: {name}")
            elif _safe_text(value).lower() not in {"", "true", "false"}:
                errors.append(f"stream option {name} must be default, true, or false")
    ros_setup = Path(_safe_text(payload.get("ros_setup")) or _default_ros_setup_for_version(ros_version))
    if not ros_setup.is_file():
        errors.append(f"ROS setup file not found: {ros_setup}")

    camera_setup = _safe_text(payload.get("camera_setup"))
    if camera_setup and not Path(camera_setup).is_file():
        errors.append(f"camera ROS setup file not found: {camera_setup}")
    if (camera_setup.endswith(".zsh") or str(ros_setup).endswith(".zsh")) and shutil.which("zsh") is None:
        errors.append("zsh setup was selected, but zsh was not found in PATH")

    for launch_arg in _parse_extra_launch_args(payload.get("launch_args")):
        if "=" not in launch_arg:
            errors.append(f"launch arg must be KEY=VALUE: {launch_arg}")
    if mode in {"restart", "stream_stall"} and not _safe_text(payload.get("duration")):
        errors.append("duration is required for this mode")

    run_count = _safe_text(payload.get("run_count")) or "1"
    try:
        if int(run_count) <= 0:
            errors.append("run_count must be > 0")
    except ValueError:
        errors.append(f"run_count must be an integer: {run_count}")

    for key in ("duration", "stable_seconds", "stream_timeout", "max_gap_seconds", "warmup_sec"):
        value = _safe_text(payload.get(key))
        if not value:
            continue
        try:
            if _duration_like_value(value) < 0.0:
                errors.append(f"{key} must be >= 0")
        except ValueError:
            errors.append(f"{key} must be numeric or use s/m/h suffix: {value}")
    restart_delay = _safe_text(payload.get("restart_delay"))
    if restart_delay:
        try:
            if float(restart_delay) < 0.0:
                errors.append("restart_delay must be >= 0")
        except ValueError:
            errors.append(f"restart_delay must be numeric: {restart_delay}")
    for key in ("warning_interval_sec", "queue_size"):
        value = _safe_text(payload.get(key))
        if not value:
            continue
        try:
            if float(value) <= 0.0:
                errors.append(f"{key} must be > 0")
        except ValueError:
            errors.append(f"{key} must be numeric: {value}")
    save_csv = _safe_text(payload.get("save_csv"))
    if save_csv and save_csv.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        errors.append(f"save_csv must be true or false: {save_csv}")
    return errors


@dataclass
class TestJob:
    run_id: str
    mode: str
    run_root: Path
    command_lines: List[str]
    shell: str
    runner_type: str = "framework"
    test_id: str = ""
    stop_policy: str = "immediate"
    standalone_rounds_supported: bool = False
    standalone_round_total: Optional[int] = None
    process: Optional[subprocess.Popen[str]] = None
    status: str = "starting"
    exit_code: Optional[int] = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    ended_at: Optional[str] = None
    logs: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_requested: bool = False
    stop_signal_sent: bool = False
    done_event: threading.Event = field(default_factory=threading.Event)

    def add_command_line(self, command_line: str) -> None:
        with self.lock:
            self.command_lines = [command_line]

    def clear_command_line(self) -> None:
        with self.lock:
            self.command_lines = []

    def add_log(self, line: str) -> None:
        text = line.rstrip("\n")
        with self.lock:
            self.logs.append(text)
            if len(self.logs) > 2000:
                self.logs = self.logs[-2000:]
        log_path = self.run_root / "ui_stdout.log"
        ensure_dir(log_path.parent)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    def snapshot(self, log_offset: int = 0) -> Dict[str, Any]:
        with self.lock:
            logs = list(self.logs)
            command_lines = list(self.command_lines)
        standalone_events = (
            last_events(self.run_root / "events.jsonl", limit=1000)
            if self.runner_type == "standalone"
            else []
        )
        elapsed_seconds = _wall_elapsed_seconds(self.started_at, self.ended_at)
        performance = (
            {"elapsed_seconds": elapsed_seconds}
            if self.runner_type == "standalone"
            else build_performance_metrics(
                self.run_root,
                started_at=self.started_at,
                ended_at=self.ended_at,
            )
        )
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "results_dir": str(self.run_root),
            "command_lines": command_lines,
            "shell": self.shell,
            "runner_type": self.runner_type,
            "test_id": self.test_id,
            "stop_policy": self.stop_policy,
            "performance": performance,
            "restart": (
                {"available": False}
                if self.runner_type == "standalone"
                else build_restart_metrics(self.run_root)
            ),
            "standalone": {
                "available": self.runner_type == "standalone",
                "test_id": self.test_id,
                "elapsed_seconds": elapsed_seconds,
                "progress": _build_standalone_progress(
                    standalone_events,
                    supported=self.standalone_rounds_supported,
                    requested_total=self.standalone_round_total,
                ),
                "events": standalone_events[-25:],
                "result": read_json(self.run_root / "result.json", {}),
            },
            "log_offset": len(logs),
            "logs": logs[log_offset:],
        }


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[TestJob] = None

    def current_snapshot(self, log_offset: int = 0) -> Dict[str, Any]:
        with self._lock:
            job = self._current
        if job is None:
            return {"status": "idle", "logs": [], "log_offset": 0}
        return job.snapshot(log_offset=log_offset)

    def is_active_run(self, run_id: str) -> bool:
        with self._lock:
            job = self._current
        return (
            job is not None
            and job.run_id == run_id
            and job.status in {"starting", "running", "stopping"}
        )

    def start(self, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            if self._current is not None and self._current.status in {"starting", "running", "stopping"}:
                return 409, {"error": "a test is already running"}

        errors = validate_run_payload(payload)
        if errors:
            return 400, {"errors": errors}

        config = save_config(
            {
                "ros_version": payload.get("ros_version") or "2",
                "ros_domain_id": normalize_ros_domain_id(payload.get("ros_domain_id")),
                "ros_setup": payload.get("ros_setup")
                or _default_ros_setup_for_version(payload.get("ros_version") or "2"),
                "camera_setup": payload.get("camera_setup")
                or _default_camera_setup_for_version(payload.get("ros_version") or "2"),
                "mode": payload.get("mode") or "functional",
                "performance_scenario": payload.get("performance_scenario") or "",
                "launch_file": payload.get("launch_file") or "",
                "launch_config": payload.get("launch_config") or "generic",
                "stream_options": payload.get("stream_options") or {},
                "run_count": payload.get("run_count") or "1",
                "continue_on_error": _bool_value(payload.get("continue_on_error")),
                "duration": payload.get("duration") or "",
                "stable_seconds": payload.get("stable_seconds") or "10",
                "stream_timeout": payload.get("stream_timeout") or "60",
                "max_gap_seconds": payload.get("max_gap_seconds") or "1.5",
                "restart_delay": payload.get("restart_delay") or "2",
                "image_topics": payload.get("image_topics") or "",
                "warning_interval_sec": payload.get("warning_interval_sec") or "1.0",
                "warmup_sec": payload.get("warmup_sec") or "2.0",
                "save_csv": payload.get("save_csv") or "true",
                "queue_size": payload.get("queue_size") or "10",
                "camera_name": payload.get("camera_name") or "",
                "serial_number": payload.get("serial_number") or "",
                "usb_port": payload.get("usb_port") or "",
                "config_file_path": payload.get("config_file_path") or "",
                "launch_args": payload.get("launch_args") or "",
            }
        )
        payload = {**payload, **config}
        mode = _safe_text(payload.get("mode")) or "functional"
        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}"
        run_root = ensure_dir(UI_RESULTS_ROOT / run_id)
        script, command_lines, shell = _build_shell_script(payload, run_root)
        write_json(
            run_root / "ui_request.json",
            {
                "run_id": run_id,
                "mode": mode,
                "request": payload,
                "command_lines": command_lines,
                "auto_test_ws": str(AUTO_TEST_WS),
            },
        )

        job = TestJob(
            run_id=run_id,
            mode=mode,
            run_root=run_root,
            command_lines=[],
            shell=shell,
        )
        with self._lock:
            self._current = job

        thread = threading.Thread(target=self._run_job, args=(job, script), daemon=True)
        thread.start()
        return 200, job.snapshot()

    def start_standalone(self, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            if self._current is not None and self._current.status in {
                "starting",
                "running",
                "stopping",
            }:
                return 409, {"error": "a test is already running"}

        test_id = _safe_text(payload.get("test_id"))
        manifest = manifest_catalog(STANDALONE_ROOT).get(test_id)
        if manifest is None:
            return 400, {"errors": [f"unsupported standalone test: {test_id}"]}
        if manifest["risk"] == "high" and payload.get("confirmed_test_id") != test_id:
            return 400, {"errors": ["high-risk confirmation is required"]}

        values, errors = validate_standalone_request(manifest, payload.get("values"))
        try:
            ros_domain_id = normalize_ros_domain_id(payload.get("ros_domain_id"))
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            return 400, {"errors": errors}

        config = load_config()
        standalone_configs = dict(config.get("standalone_configs") or {})
        standalone_configs[test_id] = values
        save_config(
            {
                "ros_domain_id": ros_domain_id,
                "standalone_configs": standalone_configs,
            }
        )

        base_run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_standalone_{test_id}"
        run_id, run_root = create_unique_run_dir(base_run_id)
        args, values = build_standalone_command(manifest, values, run_root)
        command_line = _quote_command(args)
        domain_command = _ros_domain_environment_command(
            values.get("ros_version"), ros_domain_id
        )
        displayed_command = (
            f"ROS_DOMAIN_ID={shlex.quote(ros_domain_id)} {command_line}"
            if domain_command and ros_domain_id
            else command_line
        )
        script_lines = [
            "set -e",
            f"cd {shlex.quote(str(AUTO_TEST_WS.parent))}",
            f"echo {shlex.quote(f'[UI] command: {displayed_command}')}",
        ]
        if domain_command:
            script_lines.append(domain_command)
        script_lines.append(f"exec {command_line}")
        script = "\n".join(script_lines)
        write_json(
            run_root / "ui_request.json",
            {
                "run_id": run_id,
                "mode": f"standalone:{test_id}",
                "runner_type": "standalone",
                "test_id": test_id,
                "request": {**values, "ros_domain_id": ros_domain_id},
                "command_lines": [displayed_command],
                "manifest_path": manifest["manifest_path"],
                "auto_test_ws": str(AUTO_TEST_WS),
            },
        )

        job = TestJob(
            run_id=run_id,
            mode=f"standalone:{test_id}",
            run_root=run_root,
            command_lines=[],
            shell="bash",
            runner_type="standalone",
            test_id=test_id,
            stop_policy=manifest["stop_policy"],
            standalone_rounds_supported=any(
                field.get("name") == "run_count" for field in manifest["fields"]
            ),
            standalone_round_total=_positive_int(values.get("run_count")),
        )
        with self._lock:
            self._current = job
        thread = threading.Thread(target=self._run_job, args=(job, script), daemon=True)
        thread.start()
        return 200, job.snapshot()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            job = self._current
        if job is None or job.status not in {"starting", "running", "stopping"}:
            return {"status": "idle"}

        first_request = not job.stop_requested
        job.stop_requested = True
        job.status = "stopping"
        if first_request:
            if job.stop_policy == "safe-point":
                job.add_log("[UI] requesting safe-point stop with SIGINT")
            else:
                job.add_log("[UI] stopping test with SIGINT")
        self._send_requested_stop(job)
        return job.snapshot()

    @staticmethod
    def _signal_process(job: TestJob, sig: signal.Signals, *, process_group: bool) -> None:
        process = job.process
        if process is None or process.poll() is not None:
            return
        try:
            if process_group:
                os.killpg(os.getpgid(process.pid), sig)
            else:
                process.send_signal(sig)
        except OSError:
            pass

    def _send_requested_stop(self, job: TestJob) -> None:
        with job.lock:
            if job.process is None or job.stop_signal_sent:
                return
            job.stop_signal_sent = True
        self._signal_process(
            job,
            signal.SIGINT,
            process_group=job.stop_policy != "safe-point",
        )

    def shutdown(self, timeout: float = 10.0, *, force: bool = False) -> bool:
        """Stop the active job and wait until its status/results are finalized."""
        with self._lock:
            job = self._current
        if job is None or job.status not in {"starting", "running", "stopping"}:
            return True

        self.stop()
        if force:
            job.add_log("[UI] forcing shutdown with SIGTERM")
            self._signal_process(job, signal.SIGTERM, process_group=True)
        elif job.stop_policy == "safe-point":
            job.add_log("[UI] waiting for the current operation to reach a safe point")
            job.done_event.wait()
            return True

        if job.done_event.wait(timeout=max(0.0, timeout)):
            return True

        job.add_log("[UI] test did not stop in time; sending SIGTERM")
        self._signal_process(job, signal.SIGTERM, process_group=True)
        if job.done_event.wait(timeout=5.0):
            return True

        job.add_log("[UI] test did not terminate; sending SIGKILL")
        self._signal_process(job, signal.SIGKILL, process_group=True)
        return job.done_event.wait(timeout=5.0)

    def _run_job(self, job: TestJob, script: str) -> None:
        if not job.stop_requested:
            job.status = "running"
        job.add_log(f"[UI] run id: {job.run_id}")
        job.add_log(f"[UI] results dir: {job.run_root}")
        job.add_log(f"[UI] shell: {job.shell}")

        try:
            process = subprocess.Popen(
                [job.shell, "-lc", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with job.lock:
                job.process = process
            if job.stop_requested:
                self._send_requested_stop(job)
            assert process.stdout is not None
            for line in process.stdout:
                text = line.rstrip("\n")
                if text.startswith("[UI] command: "):
                    job.add_command_line(text.removeprefix("[UI] command: ").strip())
                elif text == "[UI] command done":
                    job.clear_command_line()
                job.add_log(line)
            job.exit_code = process.wait()
            if job.runner_type == "standalone":
                result, contract_errors = validate_result_contract(
                    job.run_root / "result.json",
                    job.test_id,
                )
                if contract_errors:
                    job.status = "failed"
                    for error in contract_errors:
                        job.add_log(f"[UI] result contract error: {error}")
                elif job.status == "stopping" or job.exit_code == 130:
                    job.status = "interrupted"
                elif job.exit_code != 0:
                    job.status = "failed"
                else:
                    job.status = str(result["status"])
            elif job.status == "stopping" or job.exit_code == 130:
                job.status = "interrupted"
            elif job.exit_code == 0:
                job.status = "passed"
            elif job.exit_code == 2:
                job.status = "warning"
            else:
                job.status = "failed"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.exit_code = 1
            job.add_log(f"[UI] failed to run test: {exc}")
        finally:
            job.ended_at = datetime.now().isoformat(timespec="seconds")
            try:
                write_json(
                    job.run_root / "ui_status.json",
                    {
                        "run_id": job.run_id,
                        "mode": job.mode,
                        "status": job.status,
                        "exit_code": job.exit_code,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "results_dir": str(job.run_root),
                        "command_lines": job.command_lines,
                        "shell": job.shell,
                        "runner_type": job.runner_type,
                        "test_id": job.test_id,
                        "stop_policy": job.stop_policy,
                    },
                )
                job.add_log(
                    f"[UI] finished with status={job.status}, exit_code={job.exit_code}"
                )
            finally:
                job.done_event.set()
