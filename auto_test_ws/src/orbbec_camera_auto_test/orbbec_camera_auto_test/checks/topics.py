from __future__ import annotations

from typing import Any, Dict, List

from ..core.reporter import append_log
from ..core.ros_utils import make_qos_profile, resolve_message_type
from ..profile.loader import TopicSpec
from .topic_validators import validate_topic_message


def _emit_status(emit_status, message: str) -> None:
    if emit_status is not None:
        emit_status(message)


def run_topic_checks(harness, topic_specs: List[TopicSpec], log_path, emit_status=None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    cached_messages: Dict[str, Any] = {}

    for spec in topic_specs:
        append_log(log_path, f"[TOPIC] Checking {spec.name} ({spec.type})")
        _emit_status(
            emit_status,
            f"[TOPIC] checking {spec.name} (mode={spec.mode}, timeout={spec.timeout}s)",
        )
        result = {"name": spec.name, "type": spec.type, "status": "passed", "message": ""}
        try:
            if spec.mode == "advertised":
                harness.wait_for_topic(spec.name, topic_type=spec.type or None, timeout=spec.timeout)
                result["message"] = "topic advertised"
            else:
                message = harness.wait_for_message(
                    spec.name,
                    resolve_message_type(spec.type, harness.ros_version),
                    timeout=spec.timeout,
                    qos_profile=make_qos_profile(spec.qos, harness.ros_version),
                )
                cached_messages[spec.name] = message
                result["metrics"] = validate_topic_message(harness, spec, message, cached_messages)
            append_log(log_path, f"[TOPIC] PASS {spec.name}")
            _emit_status(emit_status, f"[TOPIC][PASS] {spec.name}")
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["message"] = str(exc)
            append_log(log_path, f"[TOPIC] FAIL {spec.name}: {exc}")
            _emit_status(emit_status, f"[TOPIC][FAIL] {spec.name}: {exc}")
        results.append(result)
    return results
