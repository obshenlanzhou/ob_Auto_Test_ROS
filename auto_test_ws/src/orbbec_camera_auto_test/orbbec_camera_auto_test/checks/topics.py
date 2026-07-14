from __future__ import annotations

from typing import Any, Dict, List

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


def _wait_for_paired_message(harness, spec: TopicSpec, cached_messages: Dict[str, Any]):
    def cache_target_message(message) -> None:
        cached_messages[spec.name] = message

    def cache_paired_message(message) -> None:
        cached_messages[spec.paired_topic] = message

    target_subscription = harness.node.create_subscription(
        resolve_message_type(spec.type, harness.ros_version),
        spec.name,
        cache_target_message,
        make_qos_profile(spec.qos, harness.ros_version),
    )
    paired_subscription = harness.node.create_subscription(
        resolve_message_type("sensor_msgs/msg/Image", harness.ros_version),
        spec.paired_topic,
        cache_paired_message,
        make_qos_profile(spec.qos, harness.ros_version),
    )
    try:
        harness.spin_until(
            lambda: spec.paired_topic in cached_messages,
            spec.timeout,
            f"message on paired topic {spec.paired_topic}",
        )
        harness.spin_until(
            lambda: spec.name in cached_messages,
            spec.timeout,
            f"message on topic {spec.name}",
        )
        return cached_messages[spec.name]
    finally:
        harness.node.destroy_subscription(paired_subscription)
        harness.node.destroy_subscription(target_subscription)


def _wait_for_validated_message(harness, spec: TopicSpec, cached_messages: Dict[str, Any]):
    state = {"matched": False, "message": None, "metrics": None, "error": None}

    def validate_message(message) -> None:
        try:
            metrics = validate_topic_message(harness, spec, message, cached_messages)
        except Exception as exc:  # noqa: BLE001
            state["error"] = exc
            return
        state.update(matched=True, message=message, metrics=metrics, error=None)

    subscription = harness.node.create_subscription(
        resolve_message_type(spec.type, harness.ros_version),
        spec.name,
        validate_message,
        make_qos_profile(spec.qos, harness.ros_version),
    )
    try:
        try:
            harness.spin_until(
                lambda: state["matched"], spec.timeout, f"valid message on {spec.name}"
            )
        except TimeoutError:
            if state["error"] is not None:
                raise state["error"]
            raise
        return state["message"], state["metrics"]
    finally:
        harness.node.destroy_subscription(subscription)


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
                harness.wait_for_topic(
                    spec.name, topic_type=spec.type or None, timeout=spec.timeout
                )
                result["message"] = "topic advertised"
            elif spec.validator == "tf_static":
                message, metrics = _wait_for_validated_message(
                    harness, spec, cached_messages
                )
                cached_messages[spec.name] = message
                result["metrics"] = metrics
            else:
                if spec.paired_topic:
                    message = _wait_for_paired_message(harness, spec, cached_messages)
                else:
                    message = harness.wait_for_message(
                        spec.name,
                        resolve_message_type(spec.type, harness.ros_version),
                        timeout=spec.timeout,
                        qos_profile=make_qos_profile(spec.qos, harness.ros_version),
                    )
                cached_messages[spec.name] = message
                result["metrics"] = validate_topic_message(
                    harness, spec, message, cached_messages
                )
            append_log(log_path, f"[TOPIC] PASS {spec.name}")
            _emit_status(emit_status, f"[TOPIC][PASS] {spec.name}")
        except Exception as exc:  # noqa: BLE001
            unsupported_reason = _topic_support_reason(harness, spec)
            if unsupported_reason:
                _mark_topic_skipped(result, unsupported_reason)
                append_log(log_path, f"[TOPIC] SKIP {spec.name}: {unsupported_reason}")
                _emit_status(emit_status, f"[TOPIC][SKIP] {spec.name}: {unsupported_reason}")
            else:
                result["status"] = "failed"
                result["message"] = str(exc)
                append_log(log_path, f"[TOPIC] FAIL {spec.name}: {exc}")
                _emit_status(emit_status, f"[TOPIC][FAIL] {spec.name}: {exc}")
        results.append(result)
    return results
