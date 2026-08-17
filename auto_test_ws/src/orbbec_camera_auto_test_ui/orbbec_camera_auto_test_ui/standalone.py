from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


MANIFEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
RESULT_STATUSES = {"passed", "failed", "interrupted"}
FIELD_TYPES = {
    "text",
    "path",
    "duration",
    "integer",
    "number",
    "select",
    "flag",
    "boolean",
    "list",
    "camera-list",
}
FIELD_GROUPS = {"environment", "configuration", "cameras", "limits", "advanced"}
CAMERA_FIELDS = (
    "name",
    "serial-number",
    "usb-port",
    "device-ip",
    "device-port",
    "config-file-path",
)
REQUIRED_RESULT_KEYS = {
    "schema_version",
    "run_id",
    "test_id",
    "status",
    "started_at",
    "ended_at",
    "duration_seconds",
    "request",
    "summary",
    "warnings",
    "details",
    "artifacts",
    "error",
}
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smh]?$", re.IGNORECASE)


class ManifestError(ValueError):
    pass


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _list_values(value: Any) -> List[str]:
    if isinstance(value, list):
        return [_safe_text(item) for item in value if _safe_text(item)]
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _safe_text(value).lower() in {"1", "true", "yes", "on"}


def _field_active(field: Dict[str, Any], values: Dict[str, Any]) -> bool:
    condition = field.get("when")
    if not isinstance(condition, dict):
        return True
    return all(
        _safe_text(values.get(name)) == _safe_text(expected)
        for name, expected in condition.items()
    )


def _validate_manifest(manifest: Dict[str, Any], manifest_path: Path) -> Dict[str, Any]:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"{manifest_path}: unsupported schema_version")
    test_id = _safe_text(manifest.get("id"))
    if not test_id or not re.fullmatch(r"[a-z0-9_]+", test_id):
        raise ManifestError(f"{manifest_path}: invalid id")
    script_name = _safe_text(manifest.get("script"))
    script_path = (manifest_path.parent / script_name).resolve()
    if not script_name or manifest_path.parent.resolve() not in script_path.parents:
        raise ManifestError(f"{manifest_path}: invalid script")
    if not script_path.is_file():
        raise ManifestError(f"{manifest_path}: script not found: {script_name}")
    if manifest.get("risk") not in {"normal", "high"}:
        raise ManifestError(f"{manifest_path}: risk must be normal or high")
    if manifest.get("stop_policy") not in {"immediate", "safe-point"}:
        raise ManifestError(f"{manifest_path}: invalid stop_policy")

    seen_names = set()
    seen_options = set()
    fields = manifest.get("fields")
    if not isinstance(fields, list):
        raise ManifestError(f"{manifest_path}: fields must be a list")
    for field in fields:
        if not isinstance(field, dict):
            raise ManifestError(f"{manifest_path}: field must be an object")
        name = _safe_text(field.get("name"))
        option = _safe_text(field.get("option"))
        field_type = _safe_text(field.get("type"))
        field_group = _safe_text(field.get("group"))
        label_en = _safe_text(field.get("label_en"))
        if not name or name in seen_names:
            raise ManifestError(f"{manifest_path}: duplicate or empty field name: {name}")
        if not option.startswith("--") or "_" in option:
            raise ManifestError(f"{manifest_path}: option must use kebab-case: {option}")
        if option in seen_options:
            raise ManifestError(f"{manifest_path}: duplicate option: {option}")
        if field_type not in FIELD_TYPES:
            raise ManifestError(f"{manifest_path}: unsupported field type: {field_type}")
        if field_group not in FIELD_GROUPS:
            raise ManifestError(f"{manifest_path}: unsupported field group: {field_group}")
        expected_section = "advanced" if field_group == "advanced" else "basic"
        if field.get("section") != expected_section:
            raise ManifestError(
                f"{manifest_path}: field {name} group {field_group} must use section {expected_section}"
            )
        if not label_en:
            raise ManifestError(f"{manifest_path}: field {name} requires label_en")
        if field_type == "select" and not field.get("choices"):
            raise ManifestError(f"{manifest_path}: select field {name} has no choices")
        seen_names.add(name)
        seen_options.add(option)

    required_any = manifest.get("required_any", [])
    if not isinstance(required_any, list):
        raise ManifestError(f"{manifest_path}: required_any must be a list")
    for requirement in required_any:
        if not isinstance(requirement, dict):
            raise ManifestError(f"{manifest_path}: required_any entry must be an object")
        names = requirement.get("fields")
        if (
            not isinstance(names, list)
            or len(names) < 2
            or any(not isinstance(name, str) or name not in seen_names for name in names)
        ):
            raise ManifestError(
                f"{manifest_path}: required_any fields must reference at least two fields"
            )
        if not _safe_text(requirement.get("message")):
            raise ManifestError(f"{manifest_path}: required_any entry requires a message")

    result = dict(manifest)
    result["manifest_path"] = str(manifest_path)
    result["script_path"] = str(script_path)
    return result


def load_manifests(standalone_root: Path) -> List[Dict[str, Any]]:
    manifests = []
    ids = set()
    for path in sorted(standalone_root.glob("*/ui_manifest.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"{path}: {exc}") from exc
        manifest = _validate_manifest(raw, path)
        if manifest["id"] in ids:
            raise ManifestError(f"duplicate standalone test id: {manifest['id']}")
        ids.add(manifest["id"])
        manifests.append(manifest)
    return manifests


def manifest_catalog(standalone_root: Path) -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in load_manifests(standalone_root)}


def public_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_path", "script_path"}
    }


def default_values(manifest: Dict[str, Any]) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    for field in manifest["fields"]:
        if "default" in field:
            defaults[field["name"]] = field["default"]
        elif field["type"] in {"list", "camera-list"}:
            defaults[field["name"]] = []
        elif field["type"] == "flag":
            defaults[field["name"]] = False
        else:
            defaults[field["name"]] = ""
    return defaults


def normalize_values(manifest: Dict[str, Any], raw_values: Any) -> Dict[str, Any]:
    source = raw_values if isinstance(raw_values, dict) else {}
    values = default_values(manifest)
    for field in manifest["fields"]:
        name = field["name"]
        if name not in source:
            continue
        field_type = field["type"]
        raw = source[name]
        if field_type == "list":
            values[name] = _list_values(raw)
        elif field_type == "camera-list":
            values[name] = raw if isinstance(raw, list) else []
        elif field_type in {"flag", "boolean"}:
            values[name] = _bool_value(raw)
        else:
            values[name] = _safe_text(raw)
    return values


def _camera_specs(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    specs = []
    for camera in value:
        if not isinstance(camera, dict):
            continue
        parts = []
        for name in CAMERA_FIELDS:
            text = _safe_text(camera.get(name))
            if text:
                parts.append(f"{name}={text}")
        if parts:
            specs.append(",".join(parts))
    return specs


def validate_request(manifest: Dict[str, Any], raw_values: Any) -> tuple[Dict[str, Any], List[str]]:
    values = normalize_values(manifest, raw_values)
    errors: List[str] = []
    fields = {field["name"]: field for field in manifest["fields"]}
    for name, field in fields.items():
        if not _field_active(field, values):
            continue
        value = values.get(name)
        field_type = field["type"]
        empty = value in ("", None, []) or (field_type == "flag" and value is False)
        if field.get("required") and empty:
            errors.append(f"{field.get('label', name)} is required")
            continue
        if empty:
            continue
        if field_type in {"integer", "number"}:
            try:
                number = int(value) if field_type == "integer" else float(value)
            except (TypeError, ValueError):
                errors.append(f"{field.get('label', name)} must be numeric")
                continue
            if "min" in field and number < field["min"]:
                errors.append(f"{field.get('label', name)} must be >= {field['min']}")
            if "max" in field and number > field["max"]:
                errors.append(f"{field.get('label', name)} must be <= {field['max']}")
        elif field_type == "duration" and not _DURATION_RE.fullmatch(_safe_text(value)):
            errors.append(f"{field.get('label', name)} must be seconds or use s/m/h suffix")
        elif field_type == "select":
            choices = {
                _safe_text(choice.get("value") if isinstance(choice, dict) else choice)
                for choice in field["choices"]
            }
            if _safe_text(value) not in choices:
                errors.append(f"{field.get('label', name)} has an unsupported value")
        elif field_type == "camera-list":
            specs = _camera_specs(value)
            if field.get("required") and not specs:
                errors.append(f"{field.get('label', name)} requires at least one camera")
            maximum = field.get("max_items")
            if maximum is not None and len(specs) > int(maximum):
                errors.append(f"{field.get('label', name)} accepts at most {maximum} camera(s)")
            if field.get("config_file_required"):
                for index, camera in enumerate(value, start=1):
                    if isinstance(camera, dict) and any(
                        _safe_text(camera.get(item)) for item in CAMERA_FIELDS
                    ) and not _safe_text(camera.get("config-file-path")):
                        errors.append(
                            f"{field.get('label', name)} #{index} requires config-file-path"
                        )
    for requirement in manifest.get("required_any", []):
        names = requirement["fields"]
        if not any(values.get(name) not in ("", None, [], False) for name in names):
            errors.append(requirement["message"])
    return values, errors


def build_command(
    manifest: Dict[str, Any],
    raw_values: Any,
    results_dir: Path,
) -> tuple[List[str], Dict[str, Any]]:
    values, errors = validate_request(manifest, raw_values)
    if errors:
        raise ValueError("\n".join(errors))
    args = [sys.executable or "python3", manifest["script_path"]]
    for field in manifest["fields"]:
        if not _field_active(field, values):
            continue
        name = field["name"]
        option = field["option"]
        field_type = field["type"]
        value = values.get(name)
        if field_type == "flag":
            if value:
                args.append(option)
        elif field_type == "boolean":
            args.extend([option, "true" if value else "false"])
        elif field_type == "list":
            for item in value or []:
                args.extend([option, str(item)])
        elif field_type == "camera-list":
            for spec in _camera_specs(value):
                args.extend([option, spec])
        elif _safe_text(value):
            args.extend([option, _safe_text(value)])
    args.extend(["--results-dir", str(results_dir)])
    return args, values


def validate_result_contract(
    result_path: Path,
    expected_test_id: str,
) -> tuple[Dict[str, Any], List[str]]:
    errors = []
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ["result.json is missing"]
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"result.json is invalid: {exc}"]
    if not isinstance(payload, dict):
        return {}, ["result.json root must be an object"]
    missing = sorted(REQUIRED_RESULT_KEYS.difference(payload))
    if missing:
        errors.append(f"result.json missing fields: {', '.join(missing)}")
    if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        errors.append("result.json has an unsupported schema_version")
    if payload.get("test_id") != expected_test_id:
        errors.append(
            f"result.json test_id mismatch: expected {expected_test_id}, "
            f"got {payload.get('test_id')}"
        )
    if payload.get("status") not in RESULT_STATUSES:
        errors.append(f"result.json has an invalid status: {payload.get('status')}")
    for key, expected_type in (
        ("request", dict),
        ("environment", dict),
        ("summary", dict),
        ("warnings", list),
        ("details", dict),
        ("artifacts", list),
    ):
        if key in payload and not isinstance(payload[key], expected_type):
            errors.append(f"result.json field {key} has the wrong type")
    return payload, errors


def last_events(path: Path, limit: int = 25) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events
