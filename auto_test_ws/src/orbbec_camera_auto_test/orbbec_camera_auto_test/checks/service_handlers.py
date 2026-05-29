from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

from ..core.ros_utils import deep_getattr, resolve_service_type
from ..profile.loader import ServiceSpec


ServiceHandler = Callable[[Any, ServiceSpec], str]
SERVICE_HANDLERS: Dict[str, ServiceHandler] = {}


def register_service_handler(mode: str) -> Callable[[ServiceHandler], ServiceHandler]:
    def decorator(func: ServiceHandler) -> ServiceHandler:
        SERVICE_HANDLERS[mode] = func
        return func

    return decorator


def get_service_handler(mode: str) -> ServiceHandler:
    handler = SERVICE_HANDLERS.get(mode)
    if handler is None:
        raise ValueError(f"Unsupported service mode: {mode}")
    return handler


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


def _ensure_success(response: Any, fallback_message: str) -> None:
    if hasattr(response, "success") and not response.success:
        raise ValueError(getattr(response, "message", fallback_message))


@register_service_handler("advertised")
def check_service_advertised(harness, spec: ServiceSpec) -> str:
    service_type = resolve_service_type(spec.type, harness.ros_version)
    harness.wait_for_service(spec.name, service_type, timeout=30.0)
    return "service advertised"


@register_service_handler("read")
def check_service_read(harness, spec: ServiceSpec) -> str:
    service_type = resolve_service_type(spec.type, harness.ros_version)
    response = harness.call_service(
        spec.name,
        service_type,
        request_data=spec.request,
        timeout=30.0,
    )
    evaluate_response_checks(response, spec.response_checks)
    return "read succeeded"


@register_service_handler("roundtrip_bool")
def check_roundtrip_bool(harness, spec: ServiceSpec) -> str:
    getter_type = resolve_service_type(spec.getter_type, harness.ros_version)
    getter_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
    )
    baseline_value = bool(deep_getattr(getter_response, spec.getter_field))
    target_value = not baseline_value
    setter_type = resolve_service_type(spec.type, harness.ros_version)
    set_response = harness.call_service(
        spec.name,
        setter_type,
        request_data={spec.request_field: target_value},
        timeout=30.0,
    )
    _ensure_success(set_response, "setter returned success=false")
    if spec.wait_after_call > 0:
        time.sleep(spec.wait_after_call)
    updated_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
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
    _ensure_success(restore_response, "restore returned success=false")
    if spec.wait_after_call > 0:
        time.sleep(spec.wait_after_call)
    restored_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
    )
    restored_value = bool(deep_getattr(restored_response, spec.getter_field))
    if restored_value != baseline_value:
        raise ValueError("state did not recover to baseline")
    return f"roundtrip succeeded (baseline={baseline_value})"


@register_service_handler("roundtrip_int")
def check_roundtrip_int(harness, spec: ServiceSpec) -> str:
    getter_type = resolve_service_type(spec.getter_type, harness.ros_version)
    getter_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
    )
    baseline_value = int(deep_getattr(getter_response, spec.getter_field))
    request_data, target_value = _resolve_roundtrip_request(spec, baseline_value)
    setter_type = resolve_service_type(spec.type, harness.ros_version)
    set_response = harness.call_service(
        spec.name,
        setter_type,
        request_data=request_data,
        timeout=30.0,
    )
    _ensure_success(set_response, "setter returned success=false")
    if spec.wait_after_call > 0:
        time.sleep(spec.wait_after_call)
    updated_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
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
    _ensure_success(restore_response, "restore returned success=false")
    if spec.wait_after_call > 0:
        time.sleep(spec.wait_after_call)
    restored_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
    )
    restored_value = int(deep_getattr(restored_response, spec.getter_field))
    if restored_value != baseline_value:
        raise ValueError("state did not recover to baseline")
    return f"roundtrip succeeded (baseline={baseline_value}, target={target_value})"


@register_service_handler("roundtrip_bool_int")
def check_roundtrip_bool_int(harness, spec: ServiceSpec) -> str:
    getter_type = resolve_service_type(spec.getter_type, harness.ros_version)
    getter_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
    )
    baseline_value = bool(int(deep_getattr(getter_response, spec.getter_field)))
    target_value = not baseline_value
    setter_type = resolve_service_type(spec.type, harness.ros_version)
    set_response = harness.call_service(
        spec.name,
        setter_type,
        request_data={spec.request_field: target_value},
        timeout=30.0,
    )
    _ensure_success(set_response, "setter returned success=false")
    if spec.wait_after_call > 0:
        time.sleep(spec.wait_after_call)
    updated_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
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
    _ensure_success(restore_response, "restore returned success=false")
    if spec.wait_after_call > 0:
        time.sleep(spec.wait_after_call)
    restored_response = harness.call_service(
        spec.getter_name,
        getter_type,
        request_data={},
        timeout=30.0,
    )
    restored_value = bool(int(deep_getattr(restored_response, spec.getter_field)))
    if restored_value != baseline_value:
        raise ValueError("state did not recover to baseline")
    return f"roundtrip succeeded (baseline={baseline_value})"
