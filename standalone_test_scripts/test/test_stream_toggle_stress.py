from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_stream_off_and_on_preview_times_default_to_four_and_keep_legacy_aliases():
    module = load_script()

    defaults = module.validate_args(
        module.parse_args(["--launch-file", "test.launch.py", "--run-count", "1"])
    )
    configured = module.validate_args(
        module.parse_args(
            [
                "--launch-file",
                "test.launch.py",
                "--run-count",
                "1",
                "--stream-off-seconds",
                "2.5",
                "--stream-on-preview-seconds",
                "7",
            ]
        )
    )
    legacy = module.parse_args(
        [
            "--launch-file",
            "test.launch.py",
            "--stop-stable-seconds",
            "3",
            "--stable-seconds",
            "8",
        ]
    )

    assert defaults["stream_off"] == 4.0
    assert defaults["stream_on_preview"] == 4.0
    assert configured["stream_off"] == 2.5
    assert configured["stream_on_preview"] == 7.0
    assert legacy.stream_off_seconds == "3"
    assert legacy.stream_on_preview_seconds == "8"


def test_save_image_topics_accept_raw_and_matching_compressed_sources():
    module = load_script()

    config = module.validate_args(
        module.parse_args(
            [
                "--launch-file",
                "test.launch.py",
                "--run-count",
                "1",
                "--image-topic",
                "/camera/color/image_raw",
                "--save-image-topic",
                "/camera/color/image_raw",
                "--save-image-topic",
                "/camera/color/image_raw/compressed",
            ]
        )
    )

    assert config["save_image_topics"] == [
        "/camera/color/image_raw",
        "/camera/color/image_raw/compressed",
    ]


def test_stream_profile_parser_supports_nested_namespace_and_camera_placeholder():
    module = load_script()

    nested = module.parse_stream_profile_spec(
        "/rig/camera_01/color/image_raw=1280x720@30:mjpg"
    )
    templated = module.parse_stream_profile_spec(
        "/{camera}/depth/image_raw=640X480@15", "camera_02"
    )

    assert (nested.camera_namespace, nested.stream) == ("/rig/camera_01", "color")
    assert (nested.width, nested.height, nested.fps) == (1280, 720, 30)
    assert nested.format == "MJPG"
    assert templated.topic == "/camera_02/depth/image_raw"
    assert (templated.width, templated.height, templated.fps) == (640, 480, 15)
    assert templated.format == ""
    with pytest.raises(ValueError, match="WIDTHxHEIGHT@FPS"):
        module.parse_stream_profile_spec("/camera/color/image_raw=640x480")


def test_stream_profile_switch_requires_two_different_matching_sets():
    module = load_script()
    common = [
        "--launch-file",
        "test.launch.py",
        "--run-count",
        "1",
        "--switch-stream-profile",
        "1",
        "--stream-profile-a",
        "/camera/color/image_raw=1280x720@30",
    ]

    with pytest.raises(ValueError, match="requires both"):
        module.validate_args(module.parse_args(common))
    with pytest.raises(ValueError, match="same topics"):
        module.validate_args(
            module.parse_args(
                common
                + [
                    "--stream-profile-b",
                    "/camera/depth/image_raw=640x480@30",
                ]
            )
        )
    with pytest.raises(ValueError, match="must be different"):
        module.validate_args(
            module.parse_args(
                common
                + [
                    "--stream-profile-b",
                    "/camera/color/image_raw=1280x720@30",
                ]
            )
        )

    config = module.validate_args(
        module.parse_args(
            common
            + [
                "--stream-profile-b",
                "/camera/color/image_raw=640x480@15",
            ]
        )
    )
    assert config["profile_switch_enabled"] is True
    assert config["profile_sets"]["B"][0].fps == 15

    format_only = module.validate_args(
        module.parse_args(
            [
                "--launch-file",
                "test.launch.py",
                "--run-count",
                "1",
                "--switch-stream-profile",
                "1",
                "--stream-profile-a",
                "/camera/color/image_raw=640x480@30:BGR",
                "--stream-profile-b",
                "/camera/color/image_raw=640x480@30:RGB888",
            ]
        )
    )
    assert format_only["profile_sets"]["A"][0].format == "BGR"
    assert format_only["profile_sets"]["B"][0].format == "RGB888"
    with pytest.raises(ValueError, match="cannot be distinguished"):
        module.validate_args(
            module.parse_args(
                [
                    "--launch-file",
                    "test.launch.py",
                    "--run-count",
                    "1",
                    "--switch-stream-profile",
                    "1",
                    "--stream-profile-a",
                    "/camera/color/image_raw=640x480@30:MJPG",
                    "--stream-profile-b",
                    "/camera/color/image_raw=640x480@30:RGB888",
                ]
            )
        )


def test_profile_groups_require_service_and_selected_target():
    module = load_script()
    targets = [module.stream_target_from_topic("/camera_01/color/image_raw")]
    spec = module.parse_stream_profile_spec(
        "/camera_01/color/image_raw=1280x720@30"
    )
    services = {
        "/camera_01/set_stream_profile": [
            "orbbec_camera_msgs/srv/SetStreamProfile"
        ]
    }

    groups = module.build_profile_groups([spec], targets, services)

    assert len(groups) == 1
    assert groups[0].service == "/camera_01/set_stream_profile"
    assert groups[0].profiles == (spec,)
    with pytest.raises(RuntimeError, match="not advertised"):
        module.build_profile_groups([spec], targets, {})
    other = module.parse_stream_profile_spec(
        "/camera_02/color/image_raw=1280x720@30"
    )
    with pytest.raises(RuntimeError, match="not a selected target"):
        module.build_profile_groups([other], targets, services)


def test_profile_state_checks_resolution_without_fps_statistics():
    module = load_script()
    spec = module.parse_stream_profile_spec("/camera/color/image_raw=640x480@30")

    passed = module.evaluate_profile_state(
        [spec],
        [
            {
                "topic": spec.topic,
                "width": 640,
                "height": 480,
            }
        ],
    )
    bad_resolution = module.evaluate_profile_state(
        [spec],
        [
            {
                "topic": spec.topic,
                "width": 1280,
                "height": 720,
            }
        ],
    )

    assert passed["all_profiles_match"] is True
    assert bad_resolution["all_profiles_match"] is False
    assert "actual_fps" not in passed["profiles"][0]
    assert "fps_match" not in passed["profiles"][0]


def test_profile_state_does_not_match_ros_encoding_to_sdk_format():
    module = load_script()
    color = module.parse_stream_profile_spec(
        "/camera/color/image_raw=640x480@30:MJPG"
    )
    depth = module.parse_stream_profile_spec(
        "/camera/depth/image_raw=640x480@30:Y16"
    )
    snapshot = [
        {
            "topic": color.topic,
            "width": 640,
            "height": 480,
            "encoding": "rgb8",
        },
        {
            "topic": depth.topic,
            "width": 640,
            "height": 480,
            "encoding": "16UC1",
        },
    ]

    passed = module.evaluate_profile_state([color, depth], snapshot)
    snapshot[0]["encoding"] = "mono16"
    different_encoding = module.evaluate_profile_state([color, depth], snapshot)

    assert passed["all_profiles_match"] is True
    assert passed["profiles"][0]["expected_format"] == "MJPG"
    assert passed["profiles"][0]["actual_encoding"] == "rgb8"
    assert passed["profiles"][0]["format_encoding_checked"] is False
    assert different_encoding["all_profiles_match"] is True


def test_profile_service_retries_once_and_reports_degraded_success():
    module = load_script()
    spec = module.parse_stream_profile_spec("/camera/color/image_raw=640x480@30")
    group = module.StreamProfileGroup(
        camera_namespace="/camera",
        camera_name="camera",
        service="/camera/set_stream_profile",
        profiles=(spec,),
    )

    class Session:
        def assert_running(self):
            pass

    class Harness:
        calls = 0

        def call_set_stream_profile(self, service, profiles, timeout):
            assert service == group.service
            assert profiles == (spec,)
            assert timeout == 15
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("first call timed out")
            return {"success": True, "message": "success"}

    sleeps = []
    result = module.call_profile_with_retry(
        session=Session(),
        harness=Harness(),
        group=group,
        timeout=15,
        retry_delay=1,
        sleep=sleeps.append,
    )

    assert result["retried"] is True
    assert [attempt["success"] for attempt in result["attempts"]] == [False, True]
    assert sleeps == [1]


def test_ros1_profile_request_batches_multiple_streams_for_one_camera():
    module = load_script()
    specs = (
        module.parse_stream_profile_spec("/camera/color/image_raw=1280x720@30:MJPG"),
        module.parse_stream_profile_spec("/camera/depth/image_raw=640x480@15:Y16"),
    )

    class Request:
        def __init__(self):
            self.profiles = []

    class Message:
        pass

    captured = {}

    class RosPy:
        @staticmethod
        def wait_for_service(service, timeout):
            assert service == "/camera/set_stream_profile"
            assert timeout == 15

        @staticmethod
        def ServiceProxy(service, service_type):
            del service, service_type

            def invoke(request):
                captured["request"] = request
                return SimpleNamespace(success=True, message="success")

            return invoke

    harness = module.RosHarness("1", "test", 10, enable_profile_switch=True)
    harness._rospy = RosPy()
    harness._set_stream_profile_type = object
    harness._set_stream_profile_request_type = Request
    harness._stream_profile_message_type = Message

    response = harness.call_set_stream_profile(
        "/camera/set_stream_profile", specs, timeout=15
    )

    assert response == {"success": True, "message": "success"}
    messages = captured["request"].profiles
    assert [(item.stream_name, item.width, item.height, item.fps, item.format) for item in messages] == [
        ("color", 1280, 720, 30, "MJPG"),
        ("depth", 640, 480, 15, "Y16"),
    ]


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


def test_ros2_harness_uses_dedicated_single_threaded_executor(monkeypatch):
    module = load_script()
    calls = []

    class FakeNode:
        def destroy_node(self):
            calls.append(("destroy_node",))

    node = FakeNode()

    class FakeExecutor:
        def __init__(self):
            calls.append(("executor_init",))

        def add_node(self, added_node):
            assert added_node is node
            calls.append(("add_node",))

        def spin_once(self, timeout_sec):
            calls.append(("spin_once", timeout_sec))

        def remove_node(self, removed_node):
            assert removed_node is node
            calls.append(("remove_node",))

        def shutdown(self, timeout_sec):
            calls.append(("executor_shutdown", timeout_sec))

    rclpy = ModuleType("rclpy")
    rclpy.init = lambda *, args: calls.append(("rclpy_init", args))
    rclpy.create_node = lambda name: (
        calls.append(("create_node", name)) or node
    )
    rclpy.ok = lambda: True
    rclpy.shutdown = lambda: calls.append(("rclpy_shutdown",))

    executors = ModuleType("rclpy.executors")
    executors.SingleThreadedExecutor = FakeExecutor
    qos = ModuleType("rclpy.qos")
    qos.qos_profile_sensor_data = object()
    sensor_msgs = ModuleType("sensor_msgs")
    sensor_msgs_msg = ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.CompressedImage = type("CompressedImage", (), {})
    sensor_msgs_msg.Image = type("Image", (), {})
    sensor_msgs_msg.Imu = type("Imu", (), {})
    sensor_msgs_msg.PointCloud2 = type("PointCloud2", (), {})
    std_srvs = ModuleType("std_srvs")
    std_srvs_srv = ModuleType("std_srvs.srv")
    std_srvs_srv.SetBool = type("SetBool", (), {})

    for name, fake_module in {
        "rclpy": rclpy,
        "rclpy.executors": executors,
        "rclpy.qos": qos,
        "sensor_msgs": sensor_msgs,
        "sensor_msgs.msg": sensor_msgs_msg,
        "std_srvs": std_srvs,
        "std_srvs.srv": std_srvs_srv,
    }.items():
        monkeypatch.setitem(sys.modules, name, fake_module)

    with module.RosHarness("2", "stream_toggle_test", 10) as harness:
        harness.spin_once(0.25)
        assert isinstance(harness._executor, FakeExecutor)

    assert ("rclpy_init", []) in calls
    assert ("spin_once", 0.25) in calls
    assert calls.index(("add_node",)) < calls.index(("spin_once", 0.25))
    assert calls.index(("remove_node",)) < calls.index(("destroy_node",))
    assert ("executor_shutdown", 5.0) in calls
    assert calls[-1] == ("rclpy_shutdown",)


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


def test_sdk_log_level_is_injected_without_explicit_camera():
    module = load_script()

    launch_args = module.build_stress_launch_args(None, "debug", [])

    assert launch_args["log_level"] == "debug"
    assert "log_file_name" not in launch_args


def test_camera_placeholder_requires_single_camera():
    module = load_script()
    args = module.parse_args(
        [
            "--launch-file",
            "test.launch.py",
            "--run-count",
            "1",
            "--image-topic",
            "/{camera}/color/image_raw",
        ]
    )
    with pytest.raises(ValueError, match="placeholder requires one --camera"):
        module.validate_args(args)

    args = module.parse_args(
        [
            "--launch-file",
            "test.launch.py",
            "--run-count",
            "1",
            "--camera",
            "name=camera_01",
            "--image-topic",
            "/{camera}/color/image_raw",
        ]
    )
    assert module.validate_args(args)["explicit_topics"] == [
        "/camera_01/color/image_raw"
    ]


def test_duration_and_run_count_default_empty_and_require_at_least_one():
    module = load_script()
    defaults = module.parse_args(["--launch-file", "test.launch.py"])

    assert defaults.duration == ""
    assert defaults.run_count is None
    with pytest.raises(
        ValueError, match="at least one of --duration or --run-count is required"
    ):
        module.validate_args(defaults)

    duration_only = module.validate_args(
        module.parse_args(
            ["--launch-file", "test.launch.py", "--duration", "15m"]
        )
    )
    count_only = module.validate_args(
        module.parse_args(
            ["--launch-file", "test.launch.py", "--run-count", "10"]
        )
    )
    both = module.validate_args(
        module.parse_args(
            [
                "--launch-file",
                "test.launch.py",
                "--duration",
                "1h",
                "--run-count",
                "20",
            ]
        )
    )

    assert duration_only["duration"] == 900.0
    assert count_only["duration"] is None
    assert both["duration"] == 3600.0


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


def test_profile_verification_recreates_zero_frame_subscriptions_once(monkeypatch):
    module = load_script()
    color_topic = "/camera/color/image_raw"
    depth_topic = "/camera/depth/image_raw"
    group = SimpleNamespace(
        camera_namespace="/camera",
        service="/camera/set_stream_profile",
    )

    monkeypatch.setattr(
        module,
        "call_profile_with_retry",
        lambda **_kwargs: {"success": True, "retried": False, "attempts": []},
    )
    verification_calls = []

    def wait_for_profile_state(**_kwargs):
        verification_calls.append(True)
        if len(verification_calls) == 1:
            raise module.StreamVerificationError(
                "timed out",
                {
                    "topics": [
                        {"topic": color_topic, "window_message_count": 0},
                        {"topic": depth_topic, "window_message_count": 200},
                    ]
                },
            )
        return {"all_profiles_match": True, "profiles": [], "topics": []}

    monkeypatch.setattr(module, "wait_for_profile_state", wait_for_profile_state)

    class Monitor:
        def __init__(self):
            self.reset_count = 0
            self.recreated = []

        def reset_window(self):
            self.reset_count += 1

        def recreate_subscriptions(self, topics):
            self.recreated.append(list(topics))

    monitor = Monitor()
    warnings = []
    emitted = []
    result = module.apply_profile_set(
        session=object(),
        harness=object(),
        monitor=monitor,
        groups=[group],
        specs=[],
        label="B",
        cycle_index=65,
        service_timeout=15.0,
        retry_delay=1.0,
        stable_seconds=4.0,
        max_gap_seconds=1.0,
        stream_timeout=20.0,
        warnings=warnings,
        emit=emitted.append,
    )

    assert len(verification_calls) == 2
    assert monitor.recreated == [[color_topic]]
    assert warnings[0]["action"] == "recreate-stream-subscriptions"
    assert warnings[0]["topics"] == [color_topic]
    assert "retrying once" in emitted[0]
    assert result["subscription_recovery"]["attempted"] is True


def test_profile_verification_does_not_recreate_when_frames_were_received(monkeypatch):
    module = load_script()
    topic = "/camera/color/image_raw"
    group = SimpleNamespace(
        camera_namespace="/camera",
        service="/camera/set_stream_profile",
    )
    monkeypatch.setattr(
        module,
        "call_profile_with_retry",
        lambda **_kwargs: {"success": True, "retried": False, "attempts": []},
    )

    def wait_for_profile_state(**_kwargs):
        raise module.StreamVerificationError(
            "timed out",
            {"topics": [{"topic": topic, "window_message_count": 1}]},
        )

    monkeypatch.setattr(module, "wait_for_profile_state", wait_for_profile_state)

    class Monitor:
        def reset_window(self):
            pass

        def recreate_subscriptions(self, _topics):
            raise AssertionError("subscription must not be recreated")

    with pytest.raises(module.StreamVerificationError):
        module.apply_profile_set(
            session=object(),
            harness=object(),
            monitor=Monitor(),
            groups=[group],
            specs=[],
            label="B",
            cycle_index=65,
            service_timeout=15.0,
            retry_delay=1.0,
            stable_seconds=4.0,
            max_gap_seconds=1.0,
            stream_timeout=20.0,
            warnings=[],
            emit=lambda _message: None,
        )


def test_enabled_verification_recreates_stalled_subscriptions_once(monkeypatch):
    module = load_script()
    color_topic = "/camera/color/image_raw"
    depth_topic = "/camera/depth/image_raw"
    verification_calls = []

    def wait_for_enabled_state(**_kwargs):
        verification_calls.append(True)
        if len(verification_calls) == 1:
            raise module.StreamVerificationError(
                "timed out",
                {
                    "topics": [
                        {"topic": color_topic, "window_message_count": 0},
                        {"topic": depth_topic, "window_message_count": 200},
                    ]
                },
            )
        return {"all_streams_stable": True, "topics": []}

    monkeypatch.setattr(module, "wait_for_enabled_state", wait_for_enabled_state)

    class Monitor:
        def __init__(self):
            self.reset_count = 0
            self.recreated = []

        def reset_window(self):
            self.reset_count += 1

        def recreate_subscriptions(self, topics):
            self.recreated.append(list(topics))

    monitor = Monitor()
    warnings = []
    emitted = []
    result = module.wait_for_enabled_state_with_subscription_recovery(
        session=object(),
        harness=object(),
        monitor=monitor,
        stable_seconds=5.0,
        max_gap_seconds=1.5,
        timeout=20.0,
        cycle_index=2,
        operation_label=color_topic,
        warnings=warnings,
        emit=emitted.append,
    )

    assert len(verification_calls) == 2
    assert monitor.reset_count == 1
    assert monitor.recreated == [[color_topic]]
    assert warnings[0]["action"] == "recreate-stream-subscriptions"
    assert result["subscription_recovery"]["attempted"] is True
    assert "retrying once" in emitted[0]


def test_enabled_verification_does_not_recreate_when_streams_are_stable(monkeypatch):
    module = load_script()
    expected = {"all_streams_stable": True, "topics": []}
    monkeypatch.setattr(
        module,
        "wait_for_enabled_state",
        lambda **_kwargs: dict(expected),
    )

    class Monitor:
        def __init__(self):
            self.reset_count = 0

        def reset_window(self):
            self.reset_count += 1

        def recreate_subscriptions(self, _topics):
            raise AssertionError("stable streams must not recreate subscriptions")

    monitor = Monitor()
    result = module.wait_for_enabled_state_with_subscription_recovery(
        session=object(),
        harness=object(),
        monitor=monitor,
        stable_seconds=5.0,
        max_gap_seconds=1.5,
        timeout=20.0,
        cycle_index=1,
        operation_label="all streams",
        warnings=[],
        emit=lambda _message: None,
    )

    assert monitor.reset_count == 1
    assert result["subscription_recovery"] == {"attempted": False, "topics": []}


def test_enabled_verification_preserves_recovery_details_after_retry_failure(
    monkeypatch,
):
    module = load_script()
    topic = "/camera/depth/image_raw"

    def wait_for_enabled_state(**_kwargs):
        raise module.StreamVerificationError(
            "timed out",
            {"topics": [{"topic": topic, "window_message_count": 0}]},
        )

    monkeypatch.setattr(module, "wait_for_enabled_state", wait_for_enabled_state)

    class Monitor:
        def reset_window(self):
            pass

        def recreate_subscriptions(self, topics):
            assert topics == [topic]

    with pytest.raises(module.StreamVerificationError) as raised:
        module.wait_for_enabled_state_with_subscription_recovery(
            session=object(),
            harness=object(),
            monitor=Monitor(),
            stable_seconds=5.0,
            max_gap_seconds=1.5,
            timeout=20.0,
            cycle_index=4,
            operation_label=topic,
            warnings=[],
            emit=lambda _message: None,
        )

    recovery = raised.value.details["subscription_recovery"]
    assert recovery["attempted"] is True
    assert recovery["topics"] == [topic]


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
    target = module.save_image_target_from_topic("/camera_01/depth/image_raw")
    compressed = module.save_image_target_from_topic(
        "/camera_01/depth/image_raw/compressed"
    )
    existing = tmp_path / "camera_01" / "depth" / "image_0003.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")

    sequence = module.ImagePathSequence(tmp_path)

    assert sequence.next_path(target) == (
        tmp_path / "camera_01" / "depth" / "image_0004.png"
    )
    assert sequence.next_path(compressed) == (
        tmp_path / "camera_01" / "depth" / "image_0005.jpg"
    )


def test_save_image_topics_map_strictly_to_selected_raw_streams():
    module = load_script()
    targets = [module.stream_target_from_topic("/camera_01/color/image_raw")]
    topic_types = {
        "/camera_01/color/image_raw": ["sensor_msgs/msg/Image"],
        "/camera_01/color/image_raw/compressed": [
            "sensor_msgs/msg/CompressedImage"
        ],
    }

    mapped = module.build_save_image_targets(
        targets,
        [
            "/camera_01/color/image_raw",
            "/camera_01/color/image_raw/compressed",
        ],
        topic_types,
    )

    assert [item.topic_kind for item in mapped[targets[0].topic]] == [
        "raw",
        "compressed",
    ]
    with pytest.raises(RuntimeError, match="does not match a selected stream"):
        module.build_save_image_targets(
            targets,
            ["/camera_01/depth/image_raw/compressed"],
            topic_types,
        )


def test_image_save_monitor_reuses_stream_monitor_for_raw_topics():
    module = load_script()
    raw_topic = "/camera_01/color/image_raw"
    compressed_topic = raw_topic + "/compressed"
    raw_message = object()
    compressed_message = object()

    class SharedMonitor:
        topics = [raw_topic]

        def latest(self, topic):
            assert topic == raw_topic
            return 7, raw_message

    class Harness:
        def __init__(self):
            self.created = []
            self.destroyed = []

        def create_image_subscription(self, topic, callback, topic_kind="raw"):
            subscription = (topic, callback, topic_kind)
            self.created.append(subscription)
            return subscription

        def destroy_subscription(self, subscription):
            self.destroyed.append(subscription)

    harness = Harness()
    targets = [
        module.save_image_target_from_topic(raw_topic),
        module.save_image_target_from_topic(compressed_topic),
    ]
    monitor = module.ImageSaveMonitor(
        harness,
        targets,
        shared_monitor=SharedMonitor(),
    )

    assert [subscription[0] for subscription in harness.created] == [compressed_topic]
    assert monitor.latest(raw_topic) == (7, raw_message)
    harness.created[0][1](compressed_message)
    assert monitor.latest(compressed_topic) == (1, compressed_message)

    monitor.close()
    assert harness.destroyed == harness.created


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

    clock.now = 5.0
    callbacks["/camera/depth/image_raw"](message)
    clock.now = 6.0
    callbacks["/camera/depth/image_raw"](message)
    assert monitor.topics_are_stable(
        ["/camera/depth/image_raw"], stable_seconds=2.0, max_gap_seconds=1.5
    )


def test_stream_monitor_recreates_only_selected_subscription():
    module = load_script()
    color_topic = "/camera/color/image_raw"
    depth_topic = "/camera/depth/image_raw"

    class Harness:
        def __init__(self):
            self.created = []
            self.destroyed = []

        def create_image_subscription(self, topic, callback):
            subscription = (topic, len(self.created), callback)
            self.created.append(subscription)
            return subscription

        def destroy_subscription(self, subscription):
            self.destroyed.append(subscription)

    harness = Harness()
    monitor = module.StreamMonitor(harness, [color_topic, depth_topic])
    original_color, original_depth = monitor.subscriptions

    monitor.recreate_subscriptions([color_topic])

    assert harness.destroyed == [original_color]
    assert monitor.subscriptions[0] != original_color
    assert monitor.subscriptions[1] == original_depth
