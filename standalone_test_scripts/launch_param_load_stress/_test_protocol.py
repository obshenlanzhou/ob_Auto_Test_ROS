from __future__ import annotations

import json
import threading
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
    summary: Optional[Dict[str, Any]] = None,
    artifacts: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    status = str(details.get("status") or "failed")
    if status not in RESULT_STATUSES:
        status = "failed"
    warnings = details.get("warnings")
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
        "request": json_ready(request),
        "summary": json_ready(summary or {}),
        "warnings": json_ready(warnings if isinstance(warnings, list) else []),
        "details": json_ready(details),
        "artifacts": json_ready(artifacts or []),
        "error": details.get("error"),
    }
