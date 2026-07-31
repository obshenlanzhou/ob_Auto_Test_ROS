import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orbbec_camera_auto_test.profile.loader import (
    ServiceSpec,
    TopicSpec,
    load_camera_profile,
)
from orbbec_camera_auto_test.profile.requirements import (
    load_launch_requirement_profile,
    resolve_required_interfaces,
)
from orbbec_camera_auto_test.runners.functional import (
    _required_service_unavailable_reason,
    _required_topic_unavailable_reason,
    _wait_for_camera_ready,
    run_functional_test,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = (
    PACKAGE_ROOT / "profiles" / "base" / "functional_required_interfaces.yaml"
)


def _load(launch_file: str, ros_version: str):
    return load_launch_requirement_profile(
        launch_file,
        ros_version,
        requirements_path=REQUIREMENTS_PATH,
    )


def test_ros2_launch_defaults_resolve_mandatory_interfaces() -> None:
    profile = _load("gemini_330_series.launch.py", "2")
    resolved = resolve_required_interfaces(profile, {}, "camera")

    assert profile.name == "ros2_dual_ir_sw_align"
    assert "camera_core" in resolved.matched_rules
    assert "color_stream" in resolved.matched_rules
    assert "depth_stream" in resolved.matched_rules
    assert "depth_point_cloud" in resolved.matched_rules
    assert "left_ir_stream" not in resolved.matched_rules
    assert "/camera/color/image_raw" in resolved.required_topics
    assert "/camera/get_sdk_version" in resolved.required_services


def test_launch_overrides_change_the_mandatory_interface_set() -> None:
    profile = _load("gemini_330_series.launch.py", "2")
    resolved = resolve_required_interfaces(
        profile,
        {
            "enable_left_ir": "true",
            "enable_point_cloud": "false",
            "depth_registration": "true",
        },
        "front_camera",
    )

    assert "left_ir_stream" in resolved.matched_rules
    assert "depth_point_cloud" not in resolved.matched_rules
    assert "software_aligned_depth" in resolved.matched_rules
    assert "/front_camera/left_ir/image_raw" in resolved.required_topics
    assert "/front_camera/depth/points" not in resolved.required_topics
    assert "/front_camera/depth/image_unaligned" in resolved.required_topics


def test_named_driver_config_overrides_launch_stream_defaults() -> None:
    profile = _load("gemini2L.launch.py", "2")
    resolved = resolve_required_interfaces(
        profile,
        {"config_file_path": "gemini2L_dual_ir.yaml"},
        "camera",
    )

    assert "ir_stream" not in resolved.matched_rules
    assert "left_ir_stream" in resolved.matched_rules
    assert "right_ir_stream" in resolved.matched_rules
    assert "/camera/ir/image_raw" not in resolved.required_topics
    assert "/camera/left_ir/image_raw" in resolved.required_topics
    assert "/camera/right_ir/image_raw" in resolved.required_topics


def test_existing_custom_config_file_overrides_launch_stream_defaults(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "custom.yaml"
    config_path.write_text(
        "enable_color: false\nenable_left_ir: true\n",
        encoding="utf-8",
    )
    profile = _load("gemini_330_series.launch.py", "2")
    resolved = resolve_required_interfaces(
        profile,
        {"config_file_path": str(config_path)},
        "camera",
    )

    assert "color_stream" not in resolved.matched_rules
    assert "left_ir_stream" in resolved.matched_rules


def test_unresolved_custom_config_file_is_rejected() -> None:
    profile = _load("gemini_330_series.launch.py", "2")
    with pytest.raises(ValueError, match="cannot resolve functional config file"):
        resolve_required_interfaces(
            profile,
            {"config_file_path": "unknown_custom.yaml"},
            "camera",
        )


def test_ros1_launch_resolves_ros1_specific_and_default_imu_topics() -> None:
    profile = _load("astra2.launch", "1")
    resolved = resolve_required_interfaces(profile, {}, "camera")

    assert profile.name == "ros1_astra2"
    assert "ros1_sdk_version_topic" in resolved.matched_rules
    assert "synchronized_imu" in resolved.matched_rules
    assert "/camera/sdk_version" in resolved.required_topics
    assert "/camera/gyro_accel/sample" in resolved.required_topics


def test_unknown_launch_is_rejected() -> None:
    with pytest.raises(ValueError, match="no required-interface profile"):
        _load("unprofiled_camera.launch.py", "2")


def test_functional_runner_rejects_unprofiled_launch_before_device_probe(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        results_dir=str(tmp_path),
        launch_file="unprofiled_camera.launch.py",
        camera_name=None,
        serial_number=None,
        usb_port=None,
        config_file_path=None,
        launch_arg=[],
        ros_version="2",
        ros_setup=None,
        driver_setup=None,
    )

    assert run_functional_test(args) == 1
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert result["status"] == "failed"
    assert "no required-interface profile" in result["preflight_error"]
    assert "preflight: no required-interface profile" in summary


def test_every_required_interface_exists_in_functional_catalog() -> None:
    catalog = load_camera_profile(
        "all_topics_services",
        package_root=PACKAGE_ROOT,
        profile_type="functional",
    )
    scenario = catalog.launch_scenarios[0]
    catalog_topics = {spec.name for spec in scenario.topics}
    catalog_services = {spec.name for spec in scenario.services}
    raw = yaml.safe_load(REQUIREMENTS_PATH.read_text(encoding="utf-8"))
    required_topics = {
        name
        for rule in raw["rules"]
        for name in rule.get("required_topics", [])
    }
    required_services = {
        name
        for rule in raw["rules"]
        for name in rule.get("required_services", [])
    }

    assert required_topics <= catalog_topics
    assert required_services <= catalog_services


def test_required_dependency_is_reported_as_unavailable() -> None:
    topic = TopicSpec(
        name="/camera/color/camera_info",
        paired_topic="/camera/color/image_raw",
    )
    service = ServiceSpec(
        name="/camera/set_value",
        type="pkg/srv/Set",
        mode="roundtrip_int",
        getter_name="/camera/get_value",
    )

    assert "paired topic" in _required_topic_unavailable_reason(
        topic, {topic.name}
    )
    assert "getter service" in _required_service_unavailable_reason(
        service, {service.name}
    )


def test_missing_readiness_service_does_not_bypass_requirement_validation() -> None:
    class FakeSession:
        def assert_running(self) -> None:
            return None

    class FakeHarness:
        ros_version = "2"

        def wait_for_node(self, *args, **kwargs) -> None:
            return None

        def wait_for_service(self, *args, **kwargs) -> None:
            raise TimeoutError("service did not appear")

    messages = []
    _wait_for_camera_ready(
        FakeSession(), FakeHarness(), "camera", messages.append
    )

    assert any(
        "continuing to required-interface validation" in message
        for message in messages
    )
