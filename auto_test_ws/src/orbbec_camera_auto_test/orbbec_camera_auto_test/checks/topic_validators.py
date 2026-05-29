from __future__ import annotations

from typing import Any, Callable, Dict

from ..core.ros_utils import resolve_message_type
from ..profile.loader import TopicSpec


TopicValidator = Callable[[Any, TopicSpec, Any, Dict[str, Any]], Dict[str, Any]]
TOPIC_VALIDATORS: Dict[str, TopicValidator] = {}


def register_topic_validator(name: str) -> Callable[[TopicValidator], TopicValidator]:
    def decorator(func: TopicValidator) -> TopicValidator:
        TOPIC_VALIDATORS[name] = func
        return func

    return decorator


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


def validate_topic_message(
    harness,
    spec: TopicSpec,
    message: Any,
    cached_messages: Dict[str, Any],
):
    validator = TOPIC_VALIDATORS.get(spec.validator)
    if validator is None:
        raise ValueError(f"Unsupported topic validator: {spec.validator}")
    return validator(harness, spec, message, cached_messages)


@register_topic_validator("any")
def validate_any(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, message, cached_messages
    return {}


@register_topic_validator("image")
def validate_image(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    if message.width <= 0 or message.height <= 0:
        raise ValueError("image width/height must be positive")
    return {"width": message.width, "height": message.height}


@register_topic_validator("camera_info_matches_image")
def validate_camera_info_matches_image(
    harness,
    spec: TopicSpec,
    message: Any,
    cached_messages: Dict[str, Any],
):
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
    return {"width": message.width, "height": message.height}


@register_topic_validator("tf_static")
def validate_tf_static(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    if not message.transforms:
        raise ValueError("tf_static has no transforms")
    child_frames = [transform.child_frame_id for transform in message.transforms]
    if not any(frame.endswith("_optical_frame") for frame in child_frames):
        raise ValueError("tf_static does not contain any optical frame")
    return {"transform_count": len(message.transforms)}


@register_topic_validator("device_status")
def validate_device_status(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    return {
        "device_online": bool(message.device_online),
        "connection_type": message.connection_type,
    }


@register_topic_validator("extrinsics")
def validate_extrinsics(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    if len(message.rotation) != 9 or len(message.translation) != 3:
        raise ValueError("extrinsics payload has unexpected dimensions")
    return {
        "rotation_length": len(message.rotation),
        "translation_length": len(message.translation),
    }


@register_topic_validator("point_cloud")
def validate_point_cloud(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    if message.point_step <= 0 or len(message.data) == 0:
        raise ValueError("point cloud payload is empty")
    return {"point_step": message.point_step, "data_size": len(message.data)}


@register_topic_validator("metadata")
def validate_metadata(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    if not str(message.json_data).strip():
        raise ValueError("metadata json_data is empty")
    return {"length": len(str(message.json_data))}


@register_topic_validator("imu")
def validate_imu(harness, spec: TopicSpec, message: Any, cached_messages: Dict[str, Any]):
    del harness, spec, cached_messages
    if not _message_stamp_nonzero(message):
        raise ValueError("imu message header stamp is zero")
    return {"frame_id": message.header.frame_id}


@register_topic_validator("string_non_empty")
def validate_string_non_empty(
    harness,
    spec: TopicSpec,
    message: Any,
    cached_messages: Dict[str, Any],
):
    del harness, spec, cached_messages
    if not str(message.data).strip():
        raise ValueError("string message is empty")
    return {"length": len(str(message.data))}
