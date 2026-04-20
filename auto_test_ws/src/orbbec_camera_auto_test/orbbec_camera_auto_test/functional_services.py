from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .profile_loader import ServiceSpec, TopicSpec
from .reporter import append_log
from .ros_utils import deep_getattr, make_qos_profile, resolve_message_type, resolve_service_type


def _emit_status(emit_status, message: str) -> None:
    if emit_status is not None:
        emit_status(message)


def evaluate_response_checks(response: Any, checks: List[str]) -> None:
    for check in checks:
        if check == "success_true":
            if not bool(deep_getattr(response, "success", False)):
                raise ValueError("response.success is not true")
            continue
        if check.startswith("field_non_empty:"):
            field_name = check.split(":", 1)[1]
            value = deep_getattr(response, field_name)
            if value in ("", None):
                raise ValueError(f"response field {field_name} is empty")
            continue
        if check.startswith("field_true:"):
            field_name = check.split(":", 1)[1]
            if not bool(deep_getattr(response, field_name, False)):
                raise ValueError(f"response field {field_name} is not true")
            continue
        raise ValueError(f"Unsupported response check: {check}")


def partition_service_specs(
    service_specs: List[ServiceSpec],
) -> Tuple[List[ServiceSpec], List[ServiceSpec], ServiceSpec | None]:
    reboot_service = None
    regular_services = []
    artifact_services = []
    for spec in service_specs:
        if spec.mode == "reboot":
            reboot_service = spec
        elif spec.mode == "artifact":
            artifact_services.append(spec)
        else:
            regular_services.append(spec)
    return regular_services, artifact_services, reboot_service


def _snapshot_files(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
    }


def _build_keepalive_subscriptions(harness, keepalive_topics: List[TopicSpec]):
    subscriptions = []
    received_counts: Dict[str, int] = {}

    for topic in keepalive_topics:
        received_counts[topic.name] = 0

        def callback(message, topic_name=topic.name):
            del message
            received_counts[topic_name] += 1

        subscription = harness.node.create_subscription(
            resolve_message_type(topic.type),
            topic.name,
            callback,
            make_qos_profile(topic.qos),
        )
        subscriptions.append(subscription)
    return subscriptions, received_counts


def _destroy_keepalive_subscriptions(harness, subscriptions) -> None:
    for subscription in subscriptions:
        harness.node.destroy_subscription(subscription)


def _resolve_roundtrip_request(spec: ServiceSpec, baseline_value):
    request_data = dict(spec.request)
    target_value = request_data.get(spec.request_field)
    if target_value != baseline_value:
        return request_data, target_value

    if spec.alternate_request:
        alternate_data = dict(spec.alternate_request)
        alternate_value = alternate_data.get(spec.request_field)
        if alternate_value == baseline_value:
            raise ValueError("alternate roundtrip request still matches baseline value")
        return alternate_data, alternate_value

    raise ValueError("configured roundtrip target matches baseline value; add alternate_request")


def run_service_checks(harness, service_specs: List[ServiceSpec], log_path, emit_status=None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for spec in service_specs:
        result = {"name": spec.name, "type": spec.type, "mode": spec.mode, "status": "passed"}
        append_log(log_path, f"[SERVICE] Checking {spec.name} ({spec.mode})")
        _emit_status(emit_status, f"[SERVICE] checking {spec.name} ({spec.mode})")
        try:
            if spec.mode == "advertised":
                service_type = resolve_service_type(spec.type)
                harness.wait_for_service(spec.name, service_type, timeout=30.0)
                result["message"] = "service advertised"
            elif spec.mode == "read":
                service_type = resolve_service_type(spec.type)
                response = harness.call_service(
                    spec.name, service_type, request_data=spec.request, timeout=30.0
                )
                evaluate_response_checks(response, spec.response_checks)
                result["message"] = "read succeeded"
            elif spec.mode == "roundtrip_bool":
                getter_type = resolve_service_type(spec.getter_type)
                getter_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                baseline_value = bool(deep_getattr(getter_response, spec.getter_field))
                target_value = not baseline_value
                setter_type = resolve_service_type(spec.type)
                set_response = harness.call_service(
                    spec.name,
                    setter_type,
                    request_data={spec.request_field: target_value},
                    timeout=30.0,
                )
                if hasattr(set_response, "success") and not set_response.success:
                    raise ValueError(
                        getattr(set_response, "message", "setter returned success=false")
                    )
                if spec.wait_after_call > 0:
                    time.sleep(spec.wait_after_call)
                updated_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                updated_value = bool(deep_getattr(updated_response, spec.getter_field))
                if updated_value != target_value:
                    raise ValueError("state did not change after setter call")

                restore_response = harness.call_service(
                    spec.name,
                    setter_type,
                    request_data={spec.request_field: baseline_value},
                    timeout=30.0,
                )
                if hasattr(restore_response, "success") and not restore_response.success:
                    raise ValueError(
                        getattr(restore_response, "message", "restore returned success=false")
                    )
                if spec.wait_after_call > 0:
                    time.sleep(spec.wait_after_call)
                restored_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                restored_value = bool(deep_getattr(restored_response, spec.getter_field))
                if restored_value != baseline_value:
                    raise ValueError("state did not recover to baseline")
                result["message"] = f"roundtrip succeeded (baseline={baseline_value})"
            elif spec.mode == "roundtrip_int":
                getter_type = resolve_service_type(spec.getter_type)
                getter_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                baseline_value = int(deep_getattr(getter_response, spec.getter_field))
                request_data, target_value = _resolve_roundtrip_request(spec, baseline_value)
                setter_type = resolve_service_type(spec.type)
                set_response = harness.call_service(
                    spec.name,
                    setter_type,
                    request_data=request_data,
                    timeout=30.0,
                )
                if hasattr(set_response, "success") and not set_response.success:
                    raise ValueError(
                        getattr(set_response, "message", "setter returned success=false")
                    )
                if spec.wait_after_call > 0:
                    time.sleep(spec.wait_after_call)
                updated_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                updated_value = int(deep_getattr(updated_response, spec.getter_field))
                if updated_value != int(target_value):
                    raise ValueError(
                        f"state did not change to target value {target_value}, got {updated_value}"
                    )

                restore_response = harness.call_service(
                    spec.name,
                    setter_type,
                    request_data={spec.request_field: baseline_value},
                    timeout=30.0,
                )
                if hasattr(restore_response, "success") and not restore_response.success:
                    raise ValueError(
                        getattr(restore_response, "message", "restore returned success=false")
                    )
                if spec.wait_after_call > 0:
                    time.sleep(spec.wait_after_call)
                restored_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                restored_value = int(deep_getattr(restored_response, spec.getter_field))
                if restored_value != baseline_value:
                    raise ValueError("state did not recover to baseline")
                result["message"] = (
                    f"roundtrip succeeded (baseline={baseline_value}, target={target_value})"
                )
            elif spec.mode == "roundtrip_bool_int":
                getter_type = resolve_service_type(spec.getter_type)
                getter_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                baseline_value = bool(int(deep_getattr(getter_response, spec.getter_field)))
                target_value = not baseline_value
                setter_type = resolve_service_type(spec.type)
                set_response = harness.call_service(
                    spec.name,
                    setter_type,
                    request_data={spec.request_field: target_value},
                    timeout=30.0,
                )
                if hasattr(set_response, "success") and not set_response.success:
                    raise ValueError(
                        getattr(set_response, "message", "setter returned success=false")
                    )
                if spec.wait_after_call > 0:
                    time.sleep(spec.wait_after_call)
                updated_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                updated_value = bool(int(deep_getattr(updated_response, spec.getter_field)))
                if updated_value != target_value:
                    raise ValueError("state did not change after setter call")

                restore_response = harness.call_service(
                    spec.name,
                    setter_type,
                    request_data={spec.request_field: baseline_value},
                    timeout=30.0,
                )
                if hasattr(restore_response, "success") and not restore_response.success:
                    raise ValueError(
                        getattr(restore_response, "message", "restore returned success=false")
                    )
                if spec.wait_after_call > 0:
                    time.sleep(spec.wait_after_call)
                restored_response = harness.call_service(
                    spec.getter_name, getter_type, request_data={}, timeout=30.0
                )
                restored_value = bool(int(deep_getattr(restored_response, spec.getter_field)))
                if restored_value != baseline_value:
                    raise ValueError("state did not recover to baseline")
                result["message"] = f"roundtrip succeeded (baseline={baseline_value})"
            else:
                raise ValueError(f"Unsupported service mode: {spec.mode}")
            append_log(log_path, f"[SERVICE] PASS {spec.name}")
            _emit_status(emit_status, f"[SERVICE][PASS] {spec.name}")
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["message"] = str(exc)
            append_log(log_path, f"[SERVICE] FAIL {spec.name}: {exc}")
            _emit_status(emit_status, f"[SERVICE][FAIL] {spec.name}: {exc}")
        results.append(result)
    return results


def run_artifact_service_checks(
    harness,
    service_specs: List[ServiceSpec],
    artifacts_root: Path,
    log_path,
    emit_status=None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for spec in service_specs:
        target_dir = artifacts_root / (spec.target_subdir or "")
        result = {"name": spec.name, "type": spec.type, "mode": spec.mode, "status": "passed"}
        append_log(log_path, f"[ARTIFACT] Checking {spec.name} -> {target_dir}")
        _emit_status(emit_status, f"[ARTIFACT] checking {spec.name}")
        subscriptions = []
        received_counts: Dict[str, int] = {}
        try:
            if spec.keepalive_topics:
                subscriptions, received_counts = _build_keepalive_subscriptions(
                    harness, spec.keepalive_topics
                )
                keepalive_names = ", ".join(topic.name for topic in spec.keepalive_topics)
                append_log(log_path, f"[ARTIFACT] keepalive subscriptions active: {keepalive_names}")
                _emit_status(emit_status, f"[ARTIFACT] keepalive subscriptions active: {keepalive_names}")

            before = _snapshot_files(target_dir)
            service_type = resolve_service_type(spec.type)
            response = harness.call_service(spec.name, service_type, request_data=spec.request)
            if hasattr(response, "success") and not response.success:
                raise ValueError(getattr(response, "message", "artifact service returned false"))
            if spec.wait_after_call > 0:
                deadline = time.monotonic() + spec.wait_after_call
                while time.monotonic() < deadline:
                    harness.spin_once(0.1)
            after = _snapshot_files(target_dir)
            new_files = sorted(after.difference(before))
            if len(new_files) < spec.min_new_files:
                raise ValueError(
                    f"expected at least {spec.min_new_files} new files, got {len(new_files)}"
                )
            result["new_files"] = new_files
            if received_counts:
                result["keepalive_messages"] = received_counts
            result["message"] = f"created {len(new_files)} files"
            append_log(log_path, f"[ARTIFACT] PASS {spec.name}: {len(new_files)} files")
            _emit_status(
                emit_status, f"[ARTIFACT][PASS] {spec.name}: created {len(new_files)} files"
            )
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["message"] = str(exc)
            append_log(log_path, f"[ARTIFACT] FAIL {spec.name}: {exc}")
            _emit_status(emit_status, f"[ARTIFACT][FAIL] {spec.name}: {exc}")
        finally:
            if subscriptions:
                _destroy_keepalive_subscriptions(harness, subscriptions)
        results.append(result)
    return results


def run_reboot_check(
    harness,
    reboot_spec: ServiceSpec,
    image_topics: List[TopicSpec],
    camera_name: str,
    log_path,
    emit_status=None,
) -> Dict[str, Any]:
    append_log(log_path, f"[REBOOT] Checking {reboot_spec.name}")
    _emit_status(emit_status, f"[REBOOT] checking {reboot_spec.name}")
    result = {"name": reboot_spec.name, "type": reboot_spec.type, "status": "passed", "message": ""}
    try:
        reboot_type = resolve_service_type(reboot_spec.type)
        harness.call_service(reboot_spec.name, reboot_type, request_data={}, timeout=30.0)
        time.sleep(3.0)
        harness.wait_for_service(
            f"/{camera_name}/get_sdk_version",
            resolve_service_type("orbbec_camera_msgs/srv/GetString"),
            timeout=120.0,
        )
        if not image_topics:
            raise ValueError("no image topics configured for reboot verification")
        for topic in image_topics:
            harness.wait_for_message(
                topic.name,
                resolve_message_type(topic.type),
                timeout=120.0,
            )
        verified_topics = ", ".join(topic.name for topic in image_topics)
        result["message"] = f"reboot and image stream recovery succeeded: {verified_topics}"
        append_log(log_path, "[REBOOT] PASS reboot recovery")
        _emit_status(emit_status, "[REBOOT][PASS] reboot recovery")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["message"] = str(exc)
        append_log(log_path, f"[REBOOT] FAIL reboot recovery: {exc}")
        _emit_status(emit_status, f"[REBOOT][FAIL] reboot recovery: {exc}")
    return result
