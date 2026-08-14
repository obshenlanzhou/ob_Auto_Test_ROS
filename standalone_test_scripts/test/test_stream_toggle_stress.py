from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stream_toggle_stress_test" / "stream_toggle_stress_test.py"


def load_script():
    module_name = "standalone_stream_toggle_stress_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_stream_target_mapping_supports_nested_camera_namespace():
    module = load_script()

    target = module.stream_target_from_topic("/rig/camera_01/left_ir/image_raw")

    assert target.topic == "/rig/camera_01/left_ir/image_raw"
    assert target.camera_namespace == "/rig/camera_01"
    assert target.camera_name == "rig_camera_01"
    assert target.stream == "left_ir"
    assert target.service == "/rig/camera_01/toggle_left_ir"


@pytest.mark.parametrize(
    "topic",
    ["/camera/color/compressed", "/color/image_raw", "/camera/depth/image_unaligned"],
)
def test_stream_target_mapping_rejects_nonstandard_topics(topic):
    module = load_script()

    with pytest.raises(ValueError):
        module.stream_target_from_topic(topic)


def test_auto_discovery_filters_derived_and_incompatible_services():
    module = load_script()
    topic_types = {
        "/camera/color/image_raw": ["sensor_msgs/msg/Image"],
        "/camera/depth/image_raw": ["sensor_msgs/msg/Image"],
        "/camera/depth_to_color/image_raw": ["sensor_msgs/msg/Image"],
        "/camera/color/image_raw/compressed": ["sensor_msgs/msg/CompressedImage"],
    }
    service_types = {
        "/camera/toggle_color": ["std_srvs/srv/SetBool"],
        "/camera/toggle_depth": ["other_msgs/srv/SetBool"],
    }

    targets, skipped = module.evaluate_discovery(topic_types, service_types)

    assert [target.topic for target in targets] == ["/camera/color/image_raw"]
    reasons = {item["topic"]: item["reason"] for item in skipped}
    assert "not advertised" in reasons["/camera/depth_to_color/image_raw"]
    assert "incompatible type" in reasons["/camera/depth/image_raw"]
    assert "/camera/color/image_raw/compressed" not in reasons


def test_explicit_targets_are_strict_and_deterministically_sorted():
    module = load_script()
    explicit = ["/camera_02/depth/image_raw", "/camera_01/color/image_raw"]
    topic_types = {
        topic: ["sensor_msgs/Image"]
        for topic in explicit
    }
    service_types = {
        "/camera_01/toggle_color": ["std_srvs/SetBool"],
        "/camera_02/toggle_depth": ["std_srvs/SetBool"],
    }

    targets, errors = module.evaluate_explicit_targets(
        explicit, topic_types, service_types
    )

    assert not errors
    assert [target.topic for target in targets] == [
        "/camera_01/color/image_raw",
        "/camera_02/depth/image_raw",
    ]

    _, missing_errors = module.evaluate_explicit_targets(
        ["/camera_03/color/image_raw"], topic_types, service_types
    )
    assert missing_errors == ["image topic not advertised: /camera_03/color/image_raw"]


def test_all_stream_groups_require_one_set_bool_service_per_camera():
    module = load_script()
    targets = [
        module.stream_target_from_topic("/camera_02/depth/image_raw"),
        module.stream_target_from_topic("/camera_01/depth/image_raw"),
        module.stream_target_from_topic("/camera_01/color/image_raw"),
    ]
    service_types = {
        "/camera_01/set_streams_enable": ["std_srvs/srv/SetBool"],
        "/camera_02/set_streams_enable": ["std_srvs/srv/SetBool"],
    }

    groups = module.build_stream_groups(targets, service_types)

    assert [group.camera_namespace for group in groups] == ["/camera_01", "/camera_02"]
    assert groups[0].service == "/camera_01/set_streams_enable"
    assert groups[0].topics == (
        "/camera_01/color/image_raw",
        "/camera_01/depth/image_raw",
    )
    with pytest.raises(RuntimeError, match="/camera_02/set_streams_enable"):
        module.build_stream_groups(targets, {"/camera_01/set_streams_enable": ["std_srvs/SetBool"]})


def test_toggle_mode_defaults_to_individual_and_accepts_all():
    module = load_script()

    assert module.parse_args(["--launch-file", "test.launch.py"]).toggle_mode == "individual"
    assert (
        module.parse_args(
            ["--launch-file", "test.launch.py", "--toggle-mode", "all"]
        ).toggle_mode
        == "all"
    )


def test_ros1_service_graph_uses_rosservice_types():
    module = load_script()
    harness = module.RosHarness("1", "test", 10)

    class RosService:
        @staticmethod
        def get_service_list():
            return ["/camera/toggle_color", "/camera/toggle_depth"]

        @staticmethod
        def get_service_type(name):
            if name.endswith("color"):
                return "std_srvs/SetBool"
            return "orbbec_camera_msgs/GetBool"

    harness._rosservice = RosService()

    assert harness.get_service_names_and_types() == {
        "/camera/toggle_color": ["std_srvs/SetBool"],
        "/camera/toggle_depth": ["orbbec_camera_msgs/GetBool"],
    }


def test_single_camera_args_are_injected_only_when_provided():
    module = load_script()
    camera = {
        "name": "camera_01",
        "serial_number": "SN001",
        "usb_port": "2-1",
        "device_ip": "",
        "device_port": "",
        "config_file_path": "/tmp/camera.yaml",
    }

    assert module.camera_launch_args(None) == {}
    assert module.camera_launch_args(camera) == {
        "camera_name": "camera_01",
        "serial_number": "SN001",
        "usb_port": "2-1",
        "config_file_path": "/tmp/camera.yaml",
    }


def test_camera_placeholder_requires_single_camera():
    module = load_script()
    args = module.parse_args(
        ["--launch-file", "test.launch.py", "--image-topic", "/{camera}/color/image_raw"]
    )
    with pytest.raises(ValueError, match="placeholder requires one --camera"):
        module.validate_args(args)

    args = module.parse_args(
        [
            "--launch-file",
            "test.launch.py",
            "--camera",
            "name=camera_01",
            "--image-topic",
            "/{camera}/color/image_raw",
        ]
    )
    assert module.validate_args(args)["explicit_topics"] == [
        "/camera_01/color/image_raw"
    ]


def test_toggle_retries_once_and_reports_degraded_success():
    module = load_script()
    target = module.stream_target_from_topic("/camera/color/image_raw")

    class Session:
        def assert_running(self):
            pass

    class Harness:
        calls = 0

        def call_set_bool(self, service, enabled, timeout):
            assert service == "/camera/toggle_color"
            assert enabled is False
            assert timeout == 15
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("first call timed out")
            return {"success": True, "message": "disabled"}

    sleeps = []
    result = module.call_toggle_with_retry(
        session=Session(),
        harness=Harness(),
        target=target,
        enabled=False,
        timeout=15,
        retry_delay=1,
        sleep=sleeps.append,
    )

    assert result["success"] is True
    assert result["retried"] is True
    assert [attempt["success"] for attempt in result["attempts"]] == [False, True]
    assert sleeps == [1]


def test_toggle_fails_after_exactly_two_attempts():
    module = load_script()
    target = module.stream_target_from_topic("/camera/depth/image_raw")

    class Session:
        def assert_running(self):
            pass

    class Harness:
        calls = 0

        def call_set_bool(self, service, enabled, timeout):
            self.calls += 1
            return {"success": False, "message": "busy"}

    harness = Harness()
    with pytest.raises(RuntimeError, match="after 2 service attempts"):
        module.call_toggle_with_retry(
            session=Session(),
            harness=harness,
            target=target,
            enabled=True,
            timeout=15,
            retry_delay=0,
        )
    assert harness.calls == 2


def test_all_disabled_state_requires_every_target_to_be_quiet(monkeypatch):
    module = load_script()

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class Session:
        def assert_running(self):
            pass

    class Harness:
        def spin_once(self, timeout):
            clock.now += timeout

    class Monitor:
        topics = ["/camera/color/image_raw", "/camera/depth/image_raw"]

        def topic_is_quiet(self, topic, quiet_seconds):
            if topic.endswith("color/image_raw"):
                return clock.now >= quiet_seconds
            return clock.now >= quiet_seconds + 0.5

        def snapshot(self):
            return [{"topic": topic} for topic in self.topics]

    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    result = module.wait_for_all_disabled_state(
        session=Session(),
        harness=Harness(),
        monitor=Monitor(),
        stop_stable_seconds=2.0,
        timeout=5.0,
    )

    assert result["all_streams_quiet"] is True
    assert result["elapsed_seconds"] >= 2.5


def test_image_paths_are_per_camera_stream_and_never_overwrite(tmp_path):
    module = load_script()
    target = module.stream_target_from_topic("/camera_01/depth/image_raw")
    existing = tmp_path / "camera_01" / "depth" / "image_0003.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")

    sequence = module.ImagePathSequence(tmp_path)

    assert sequence.next_path(target) == (
        tmp_path / "camera_01" / "depth" / "image_0004.jpg"
    )
    assert sequence.next_path(target) == (
        tmp_path / "camera_01" / "depth" / "image_0005.jpg"
    )


def test_stream_monitor_tracks_quiet_stability_and_max_gap(monkeypatch):
    module = load_script()

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class Harness:
        def create_image_subscription(self, topic, callback):
            return (topic, callback)

        def destroy_subscription(self, subscription):
            pass

    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monitor = module.StreamMonitor(
        Harness(), ["/camera/color/image_raw", "/camera/depth/image_raw"]
    )
    callbacks = {topic: callback for topic, callback in monitor.subscriptions}
    message = SimpleNamespace(width=640, height=480, encoding="rgb8", data=b"x")

    monitor.reset_window()
    callbacks["/camera/depth/image_raw"](message)
    clock.now = 1.0
    callbacks["/camera/depth/image_raw"](message)
    clock.now = 2.0
    callbacks["/camera/depth/image_raw"](message)

    assert monitor.topic_is_quiet("/camera/color/image_raw", 2.0)
    assert monitor.topics_are_stable(
        ["/camera/depth/image_raw"], stable_seconds=2.0, max_gap_seconds=1.5
    )

    clock.now = 4.0
    callbacks["/camera/depth/image_raw"](message)
    assert not monitor.topics_are_stable(
        ["/camera/depth/image_raw"], stable_seconds=2.0, max_gap_seconds=1.5
    )
