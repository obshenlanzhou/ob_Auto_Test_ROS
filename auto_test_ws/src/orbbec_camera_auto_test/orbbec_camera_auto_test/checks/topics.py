from __future__ import annotations

from typing import Any, Dict, Iterable, List

from ..core.reporter import append_log
from ..core.ros_utils import make_qos_profile, resolve_message_type
from ..profile.loader import TopicSpec
from .topic_validators import validate_topic_message


def _emit_status(emit_status, message: str) -> None:
    if emit_status is not None:
        emit_status(message)


def _topic_support_reason(harness, spec: TopicSpec) -> str:
    checks = [(spec.name, spec.type)]
    if spec.paired_topic:
        checks.append((spec.paired_topic, ""))

    for topic_name, type_name in checks:
        supported, reason = harness.topic_is_supported(topic_name, type_name)
        if not supported:
            return reason
    return ""


def _mark_topic_skipped(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
    result["status"] = "skipped"
    result["message"] = reason
    return result


def _is_not_advertised(reason: str) -> bool:
    return "topic not advertised:" in reason


def select_discovered_topic_specs(
    topic_specs: List[TopicSpec], discovered_names: Iterable[str]
) -> List[TopicSpec]:
    names = set(discovered_names)
    return [
        spec
        for spec in topic_specs
        if spec.name in names and (not spec.paired_topic or spec.paired_topic in names)
    ]


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
        discovery_reason = _topic_support_reason(harness, spec)
        if discovery_reason:
            if _is_not_advertised(discovery_reason):
                _mark_topic_skipped(result, discovery_reason)
                label = "SKIP"
            else:
                result["status"] = "failed"
                result["message"] = discovery_reason
                label = "FAIL"
            append_log(log_path, f"[TOPIC] {label} {spec.name}: {discovery_reason}")
            _emit_status(emit_status, f"[TOPIC][{label}] {spec.name}: {discovery_reason}")
            results.append(result)
            continue
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
