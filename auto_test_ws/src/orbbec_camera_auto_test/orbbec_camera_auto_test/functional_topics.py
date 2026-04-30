from __future__ import annotations

from typing import Any, Dict, List

from .profile_loader import TopicSpec
from .reporter import append_log
from .ros_utils import make_qos_profile, resolve_message_type


def _emit_status(emit_status, message: str) -> None:
    if emit_status is not None:
        emit_status(message)


def _message_stamp_nonzero(message: Any) -> bool:
    header = getattr(message, "header", None)
    if header is None:
        return True
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return True
    sec = getattr(stamp, "sec", getattr(stamp, "secs", 0))
    nanosec = getattr(stamp, "nanosec", getattr(stamp, "nsecs", 0))
    return bool(sec or nanosec)


def validate_topic_message(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    validator = spec.validator
    metrics: Dict[str, Any] = {}

    if validator == "any":
        return metrics
    if validator == "image":
        if message.width <= 0 or message.height <= 0:
            raise ValueError("image width/height must be positive")
        metrics.update({"width": message.width, "height": message.height})
        return metrics
    if validator == "camera_info_matches_image":
        if message.width <= 0 or message.height <= 0:
            raise ValueError("camera_info width/height must be positive")
        image_message = cached_messages.get(spec.paired_topic)
        if image_message is None:
            image_message = harness.wait_for_message(
                spec.paired_topic,
                resolve_message_type("sensor_msgs/msg/Image", harness.ros_version),
                timeout=spec.timeout,
            )
            cached_messages[spec.paired_topic] = image_message
        if message.width != image_message.width or message.height != image_message.height:
            raise ValueError("camera_info dimensions do not match image dimensions")
        metrics.update({"width": message.width, "height": message.height})
        return metrics
    if validator == "tf_static":
        if not message.transforms:
            raise ValueError("tf_static has no transforms")
        child_frames = [transform.child_frame_id for transform in message.transforms]
        if not any(frame.endswith("_optical_frame") for frame in child_frames):
            raise ValueError("tf_static does not contain any optical frame")
        metrics["transform_count"] = len(message.transforms)
        return metrics
    if validator == "device_status":
        metrics.update(
            {
                "device_online": bool(message.device_online),
                "connection_type": message.connection_type,
            }
        )
        return metrics
    if validator == "extrinsics":
        if len(message.rotation) != 9 or len(message.translation) != 3:
            raise ValueError("extrinsics payload has unexpected dimensions")
        return {
            "rotation_length": len(message.rotation),
            "translation_length": len(message.translation),
        }
    if validator == "point_cloud":
        if message.point_step <= 0 or len(message.data) == 0:
            raise ValueError("point cloud payload is empty")
        return {"point_step": message.point_step, "data_size": len(message.data)}
    if validator == "metadata":
        if not str(message.json_data).strip():
            raise ValueError("metadata json_data is empty")
        return {"length": len(str(message.json_data))}
    if validator == "imu":
        if not _message_stamp_nonzero(message):
            raise ValueError("imu message header stamp is zero")
        return {"frame_id": message.header.frame_id}
    if validator == "string_non_empty":
        if not str(message.data).strip():
            raise ValueError("string message is empty")
        return {"length": len(str(message.data))}
    raise ValueError(f"Unsupported topic validator: {validator}")


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
