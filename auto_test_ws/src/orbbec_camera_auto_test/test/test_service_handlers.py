from types import SimpleNamespace

import pytest

from orbbec_camera_auto_test.checks import service_handlers, services
from orbbec_camera_auto_test.profile.loader import ServiceSpec


def _auto_white_balance_spec(mode: str = "roundtrip_bool_int") -> ServiceSpec:
    return ServiceSpec(
        name="/camera/set_auto_white_balance",
        type="std_srvs/srv/SetBool",
        mode=mode,
        getter_name="/camera/get_auto_white_balance",
        getter_type="orbbec_camera_msgs/srv/GetInt32",
    )


def test_ros1_service_preflight_expects_set_int32() -> None:
    checked_types = {}

    class FakeHarness:
        ros_version = "1"

        def service_is_supported(self, service_name, type_name):
            checked_types[service_name] = type_name
            return True, "service advertised"

    assert services._service_support_reason(FakeHarness(), _auto_white_balance_spec()) == ""
    assert (
        checked_types["/camera/set_auto_white_balance"]
        == "orbbec_camera_msgs/srv/SetInt32"
    )


@pytest.mark.parametrize(
    ("ros_version", "expected_type", "expected_value"),
    [
        ("1", "orbbec_camera_msgs/srv/SetInt32", 1),
        ("2", "std_srvs/srv/SetBool", True),
    ],
)
def test_set_auto_white_balance_uses_ros_specific_type(
    monkeypatch: pytest.MonkeyPatch,
    ros_version: str,
    expected_type: str,
    expected_value,
) -> None:
    calls = []

    class FakeHarness:
        def __init__(self):
            self.ros_version = ros_version

        def call_service(self, name, service_type, request_data, timeout):
            calls.append((name, service_type, request_data, timeout))
            return SimpleNamespace(success=True)

    monkeypatch.setattr(service_handlers, "resolve_service_type", lambda name, version: name)

    service_handlers._set_auto_white_balance(
        FakeHarness(), "/camera/set_auto_white_balance", True
    )

    assert calls == [
        (
            "/camera/set_auto_white_balance",
            expected_type,
            {"data": expected_value},
            30.0,
        )
    ]


def test_ros1_roundtrip_auto_white_balance_uses_set_int32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    getter_values = iter((1, 0, 1))

    class FakeHarness:
        ros_version = "1"

        def call_service(self, name, service_type, request_data, timeout):
            calls.append((name, service_type, request_data, timeout))
            if name == "/camera/get_auto_white_balance":
                return SimpleNamespace(data=next(getter_values))
            return SimpleNamespace(success=True)

    monkeypatch.setattr(service_handlers, "resolve_service_type", lambda name, version: name)

    service_handlers.check_roundtrip_bool_int(
        FakeHarness(), _auto_white_balance_spec()
    )

    setter_calls = [call for call in calls if call[0] == "/camera/set_auto_white_balance"]
    assert setter_calls == [
        (
            "/camera/set_auto_white_balance",
            "orbbec_camera_msgs/srv/SetInt32",
            {"data": 0},
            30.0,
        ),
        (
            "/camera/set_auto_white_balance",
            "orbbec_camera_msgs/srv/SetInt32",
            {"data": 1},
            30.0,
        ),
    ]


def test_artifact_service_waits_for_keepalive_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = []
    keepalive_ready = {"value": False}
    target_dir = tmp_path / "artifacts" / "image"

    class FakeNode:
        def create_subscription(self, msg_type, topic_name, callback, qos_profile):
            del msg_type, topic_name, qos_profile
            callbacks.append(callback)
            return callback

        def destroy_subscription(self, subscription):
            del subscription

    class FakeHarness:
        ros_version = "1"
        node = FakeNode()

        def service_is_supported(self, service_name, type_name):
            del service_name, type_name
            return True, "service advertised"

        def spin_until(self, predicate, timeout, description):
            del timeout, description
            for callback in callbacks:
                callback(SimpleNamespace())
            keepalive_ready["value"] = predicate()

        def call_service(self, name, service_type, request_data):
            del name, service_type, request_data
            assert keepalive_ready["value"]
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "color.png").touch()
            return SimpleNamespace()

    monkeypatch.setattr(services, "resolve_message_type", lambda name, version: name)
    monkeypatch.setattr(services, "resolve_service_type", lambda name, version: name)
    spec = ServiceSpec(
        name="/camera/save_images",
        type="std_srvs/srv/Empty",
        mode="artifact",
        target_subdir="image",
        wait_after_call=0.0,
        keepalive_topics=[
            SimpleNamespace(
                name="/camera/color/image_raw",
                type="sensor_msgs/msg/Image",
                qos="default",
                timeout=10.0,
            )
        ],
    )

    result = services.run_artifact_service_checks(
        FakeHarness(), [spec], tmp_path / "artifacts", tmp_path / "service.log"
    )

    assert result[0]["status"] == "passed"
