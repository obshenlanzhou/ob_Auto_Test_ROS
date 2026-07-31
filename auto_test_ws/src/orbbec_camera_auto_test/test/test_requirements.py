import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orbbec_camera_auto_test.profile.loader import (
    CameraProfile,
    LaunchScenarioSpec,
    ServiceSpec,
    TopicSpec,
    load_camera_profile,
)
from orbbec_camera_auto_test.profile.requirements import (
    LaunchRequirementProfile,
    RequiredInterfaceRule,
    load_launch_requirement_profile,
    resolve_required_interfaces,
)
from orbbec_camera_auto_test.runners import functional
from orbbec_camera_auto_test.runners.functional import (
    _run_scenario,
    _required_service_unavailable_reason,
    _required_topic_unavailable_reason,
    _wait_for_camera_ready,
    _wait_for_required_interfaces,
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


def test_synchronized_imu_is_mandatory_when_sync_output_is_enabled() -> None:
    profile = _load("gemini_330_series.launch.py", "2")
    resolved = resolve_required_interfaces(
        profile,
        {
            "enable_sync_output_accel_gyro": "true",
            "enable_accel": "false",
            "enable_gyro": "false",
        },
        "camera",
    )

    assert "synchronized_imu" in resolved.matched_rules
    assert "/camera/gyro_accel/sample" in resolved.required_topics
    assert "/camera/accel/imu_info" in resolved.required_topics
    assert "/camera/gyro/imu_info" in resolved.required_topics


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
        def assert_healthy(self) -> None:
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


def test_missing_required_interface_stops_before_functional_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks_called = {"topics": False}

    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def assert_healthy(self) -> None:
            return None

    class FakeHarness:
        ros_version = "2"

        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def wait_for_node(self, *args, **kwargs) -> None:
            return None

        def wait_for_service(self, *args, **kwargs) -> None:
            return None

        def graph_snapshot(self):
            return {"nodes": [], "topics": [], "services": []}

    def unexpected_topic_checks(*args, **kwargs):
        checks_called["topics"] = True
        return []

    monkeypatch.setattr(functional, "TestSession", FakeSession)
    monkeypatch.setattr(functional, "RosHarness", FakeHarness)
    monkeypatch.setattr(functional, "resolve_service_type", lambda *args: object)
    monkeypatch.setattr(functional, "run_topic_checks", unexpected_topic_checks)
    monkeypatch.setattr(functional, "REQUIRED_INTERFACE_DISCOVERY_TIMEOUT", 0.0)

    profile = CameraProfile(
        profile_name="generic_functional",
        launch_file="",
        default_launch_args={},
        launch_scenarios=[],
        performance_topics=[],
        performance_scenarios=[],
    )
    scenario = LaunchScenarioSpec(
        name="default",
        topics=[TopicSpec(name="/{camera}/required")],
    )
    requirement_profile = LaunchRequirementProfile(
        name="test_launch",
        ros_version="2",
        launch_file="camera.launch.py",
        camera_models=["Test Camera"],
        defaults={},
        config_overrides={},
        rules=[
            RequiredInterfaceRule(
                name="required_stream",
                required_topics=["/{camera}/required"],
            )
        ],
    )

    result, reboot_spec = _run_scenario(
        profile=profile,
        requirement_profile=requirement_profile,
        launch_file="camera.launch.py",
        scenario=scenario,
        base_launch_args={"camera_name": "camera"},
        results_dir=tmp_path,
        launch_log_path=tmp_path / "launch.log",
        topic_log_path=tmp_path / "topic.log",
        service_log_path=tmp_path / "service.log",
        driver_setup=None,
        ros_version="2",
        ros_setup=None,
        emit_status=lambda message: None,
    )

    assert result["status"] == "failed"
    assert "stopping scenario before functional checks" in result["message"]
    assert result["topics"][0]["status"] == "failed"
    assert checks_called["topics"] is False
    assert reboot_spec is None


def test_required_interface_wait_accepts_delayed_graph_publication() -> None:
    class HealthySession:
        def assert_healthy(self) -> None:
            return None

    class DelayedGraphHarness:
        def __init__(self) -> None:
            self.snapshot_count = 0
            self.spin_count = 0

        def graph_snapshot(self):
            self.snapshot_count += 1
            topics = (
                []
                if self.snapshot_count == 1
                else [{"name": "/tf_static", "types": ["tf2_msgs/msg/TFMessage"]}]
            )
            return {"nodes": [], "topics": topics, "services": []}

        def spin_once(self, timeout: float) -> None:
            self.spin_count += 1

    scenario = LaunchScenarioSpec(
        name="default",
        topics=[TopicSpec(name="/tf_static")],
    )
    requirements = resolve_required_interfaces(
        LaunchRequirementProfile(
            name="test_launch",
            ros_version="2",
            launch_file="camera.launch.py",
            camera_models=["Test Camera"],
            defaults={},
            config_overrides={},
            rules=[
                RequiredInterfaceRule(
                    name="static_tf",
                    required_topics=["/tf_static"],
                )
            ],
        ),
        {},
        "camera",
    )
    harness = DelayedGraphHarness()
    messages = []

    snapshot, missing_topics, missing_services = _wait_for_required_interfaces(
        HealthySession(),
        harness,
        scenario,
        requirements,
        messages.append,
        timeout=1.0,
    )

    assert snapshot["topics"][0]["name"] == "/tf_static"
    assert missing_topics == {}
    assert missing_services == {}
    assert harness.spin_count == 1
    assert "all required ROS interfaces are now available" in messages
