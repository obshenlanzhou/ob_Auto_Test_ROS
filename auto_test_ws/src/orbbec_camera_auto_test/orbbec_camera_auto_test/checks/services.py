from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..core.reporter import append_log
from ..core.ros_utils import make_qos_profile, resolve_message_type, resolve_service_type
from ..profile.loader import ServiceSpec, TopicSpec
from .service_handlers import get_service_handler


def _emit_status(emit_status, message: str) -> None:
    if emit_status is not None:
        emit_status(message)


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
            resolve_message_type(topic.type, harness.ros_version),
            topic.name,
            callback,
            make_qos_profile(topic.qos, harness.ros_version),
        )
        subscriptions.append(subscription)
    return subscriptions, received_counts


def _destroy_keepalive_subscriptions(harness, subscriptions) -> None:
    for subscription in subscriptions:
        harness.node.destroy_subscription(subscription)


def run_service_checks(harness, service_specs: List[ServiceSpec], log_path, emit_status=None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for spec in service_specs:
        result = {"name": spec.name, "type": spec.type, "mode": spec.mode, "status": "passed"}
        append_log(log_path, f"[SERVICE] Checking {spec.name} ({spec.mode})")
        _emit_status(emit_status, f"[SERVICE] checking {spec.name} ({spec.mode})")
        try:
            result["message"] = get_service_handler(spec.mode)(harness, spec)
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
            service_type = resolve_service_type(spec.type, harness.ros_version)
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
        reboot_type = resolve_service_type(reboot_spec.type, harness.ros_version)
        harness.call_service(reboot_spec.name, reboot_type, request_data={}, timeout=30.0)
        time.sleep(3.0)
        harness.wait_for_service(
            f"/{camera_name}/get_sdk_version",
            resolve_service_type("orbbec_camera_msgs/srv/GetString", harness.ros_version),
            timeout=120.0,
        )
        if not image_topics:
            raise ValueError("no image topics configured for reboot verification")
        for topic in image_topics:
            harness.wait_for_message(
                topic.name,
                resolve_message_type(topic.type, harness.ros_version),
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
