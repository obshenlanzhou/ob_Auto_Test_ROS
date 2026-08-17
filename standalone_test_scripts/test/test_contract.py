from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "export_load": ROOT
    / "export_load_stress_test"
    / "export_load_stress_test.py",
    "firmware_update": ROOT
    / "firmware_update_stress_test"
    / "firmware_update_stress_test.py",
    "image_receive_stats": ROOT
    / "image_receive_stats_test"
    / "image_topic_receive_stats.py",
    "launch_param_load": ROOT
    / "launch_param_load_stress"
    / "launch_param_load_stress.py",
    "launch_restart": ROOT
    / "launch_restart_stream_check"
    / "launch_restart_stream_check.py",
    "preset_upgrade": ROOT
    / "preset_upgrade_stress_test"
    / "preset_upgrade_stress_test.py",
    "stream_toggle": ROOT
    / "stream_toggle_stress_test"
    / "stream_toggle_stress_test.py",
}
PROTOCOLS = [script.parent / "_test_protocol.py" for script in SCRIPTS.values()]
RESULT_KEYS = {
    "schema_version",
    "run_id",
    "test_id",
    "status",
    "started_at",
    "ended_at",
    "duration_seconds",
    "request",
    "environment",
    "summary",
    "warnings",
    "details",
    "artifacts",
    "error",
}
REMOVED_OPTIONS = {
    "--camera-name",
    "--test-count",
    "--repeat",
    "--save-images-count",
    "--topics",
    "--output_dir",
    "--warning_interval_sec",
    "--warmup_sec",
    "--queue_size",
    "--buff_size",
    "--save_csv",
    "--disable_csv",
    "--jpg-quality",
}
LIMITED_STRESS_SCRIPTS = {
    "export_load": [],
    "firmware_update": [],
    "launch_param_load": [
        "--launch-file",
        "test.launch.py",
        "--camera",
        "name=camera,config-file-path=/tmp/camera.yaml",
    ],
    "launch_restart": [],
    "preset_upgrade": [],
    "stream_toggle": ["--launch-file", "test.launch.py"],
}


def load_protocol(path: Path):
    module_name = f"test_protocol_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_script(path: Path):
    module_name = f"standalone_script_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def script_help(script: Path) -> str:
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=str(script.parent),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def evaluate_script(script: Path, expression: str):
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import json; import {script.stem} as module; "
            f"print(json.dumps({expression}))",
        ],
        cwd=str(script.parent),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("script_name", LIMITED_STRESS_SCRIPTS)
def test_stress_scripts_require_duration_or_run_count(script_name):
    script = SCRIPTS[script_name]
    completed = subprocess.run(
        [sys.executable, str(script), *LIMITED_STRESS_SCRIPTS[script_name]],
        cwd=str(script.parent),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert "at least one of --duration or --run-count is required" in completed.stderr


@pytest.mark.parametrize("script_name", LIMITED_STRESS_SCRIPTS)
def test_stress_script_limit_arguments_accept_either_or_both(script_name):
    module = load_script(SCRIPTS[script_name])
    base = LIMITED_STRESS_SCRIPTS[script_name]

    duration_only = module.parse_args([*base, "--duration", "15m"])
    count_only = module.parse_args([*base, "--run-count", "10"])
    both = module.parse_args(
        [*base, "--duration", "1h", "--run-count", "20"]
    )

    assert duration_only.duration == "15m"
    assert duration_only.run_count is None
    assert count_only.duration == ""
    assert count_only.run_count == 10
    assert both.duration == "1h"
    assert both.run_count == 20


def test_protocol_copies_are_identical():
    expected = PROTOCOLS[0].read_bytes()
    assert all(path.read_bytes() == expected for path in PROTOCOLS)


def test_terminal_log_tees_stdout_and_stderr(tmp_path, monkeypatch):
    protocol = load_protocol(PROTOCOLS[0])
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_stdout)
    monkeypatch.setattr(sys, "stderr", captured_stderr)

    log_path = protocol.install_terminal_log(tmp_path / "terminal.log")
    try:
        print("stdout message")
        print("stderr message", file=sys.stderr)
    finally:
        protocol.close_terminal_log()

    assert captured_stdout.getvalue() == "stdout message\n"
    assert captured_stderr.getvalue() == "stderr message\n"
    assert log_path.read_text(encoding="utf-8") == (
        "stdout message\nstderr message\n"
    )
    assert {item["path"] for item in protocol.artifact_list(tmp_path)} == {
        "terminal.log"
    }


def test_camera_spec_allows_each_field_independently_and_together():
    protocol = load_protocol(PROTOCOLS[0])
    field_values = {
        "name": "camera_01",
        "serial-number": "SN001",
        "usb-port": "2-1",
        "device-ip": "192.168.1.10",
        "device-port": "8090",
        "config-file-path": "/tmp/camera.yaml",
    }

    for public_name, value in field_values.items():
        camera = protocol.parse_camera(f"{public_name}={value}")
        internal_name = protocol.CAMERA_FIELDS[public_name]
        assert camera[internal_name] == value

    combined = protocol.parse_camera(
        ",".join(f"{name}={value}" for name, value in field_values.items())
    )
    assert combined == {
        protocol.CAMERA_FIELDS[name]: value
        for name, value in field_values.items()
    }


def test_camera_spec_rejects_unknown_or_positional_fields():
    protocol = load_protocol(PROTOCOLS[0])
    for value in ("camera_01", "unknown=value", ""):
        try:
            protocol.parse_camera(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid camera specification accepted: {value!r}")


def test_result_envelope_and_atomic_artifacts(tmp_path):
    protocol = load_protocol(PROTOCOLS[0])
    events = protocol.EventWriter(tmp_path / "events.jsonl")
    events.emit("run_started", "started", run_index=1)
    payload = protocol.contract_result(
        test_id="example",
        run_id="run-1",
        started_at="2026-01-01T00:00:00",
        ended_at="2026-01-01T00:00:02",
        request={"run_count": 1},
        details={"status": "passed", "elapsed_seconds": 2.0},
        summary={"runs_passed": 1},
    )
    protocol.atomic_write_json(tmp_path / "result.json", payload)

    persisted = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    assert set(persisted) == RESULT_KEYS
    assert persisted["status"] == "passed"
    assert persisted["duration_seconds"] == 2.0
    assert persisted["environment"]["host"]["os"]
    assert event["event"] == "run_started"
    assert event["run_index"] == 1
    assert not (tmp_path / ".result.json.tmp").exists()

    artifacts = protocol.artifact_list(tmp_path)
    assert {item["path"] for item in artifacts} == {"events.jsonl"}


def test_result_envelope_normalizes_unknown_status_to_failed():
    protocol = load_protocol(PROTOCOLS[0])
    payload = protocol.contract_result(
        test_id="example",
        run_id="run-1",
        started_at="start",
        ended_at="end",
        request={},
        details={"status": "unknown"},
    )
    assert payload["status"] == "failed"


def test_environment_contains_host_ros_and_driver_details(tmp_path, monkeypatch):
    protocol = load_protocol(PROTOCOLS[0])
    prefix = tmp_path / "driver"
    package_file = prefix / "share" / "orbbec_camera" / "package.xml"
    package_file.parent.mkdir(parents=True)
    package_file.write_text(
        "<package><name>orbbec_camera</name><version>2.9.3</version></package>",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMENT_PREFIX_PATH", str(prefix))
    monkeypatch.setenv("ROS_DISTRO", "humble")
    monkeypatch.setenv("ROS_VERSION", "2")
    monkeypatch.setattr(
        protocol.subprocess,
        "run",
        lambda *args, **kwargs: protocol.subprocess.CompletedProcess(
            args[0],
            0,
            stdout=(
                "Name: Orbbec Gemini 305\n"
                "Serial: CV2R1610002F\n"
                "Firmware version: 1.0.85\n"
                "Connection: USB\n"
                "USB port: 2-1\n"
            ),
        ),
    )

    environment = protocol.collect_test_environment(
        {"ros_version": "2", "ros_setup": "/opt/ros/humble/setup.bash"}
    )
    host = environment["host"]
    assert host["os"]
    assert host["kernel"]
    assert host["architecture"]
    assert host["logical_cpus"] > 0
    assert host["total_memory_gb"] > 0
    assert host["ros_distro"] == "humble"
    assert host["ros_version"] == "2"
    assert host["camera_driver_version"] == "2.9.3"
    assert environment["cameras"] == [
        {
            "camera_model": "Orbbec Gemini 305",
            "serial_number": "CV2R1610002F",
            "firmware_version": "1.0.85",
            "connection": "USB",
            "usb_port": "2-1",
        }
    ]

    markdown = "\n".join(protocol.test_environment_markdown(environment))
    assert "## Test Environment" in markdown
    assert "Camera driver version: `2.9.3`" in markdown
    assert "Orbbec Gemini 305" in markdown
    assert "1.0.85" in markdown


def test_all_summaries_include_test_environment_section():
    for script in SCRIPTS.values():
        source = script.read_text(encoding="utf-8")
        assert "test_environment_markdown" in source, script


def test_all_scripts_expose_kebab_case_common_options_without_ros_startup():
    for test_id, script in SCRIPTS.items():
        output = script_help(script)
        options = set(re.findall(r"(?<!\\w)--[a-zA-Z0-9_-]+", output))
        assert {"--ros-version", "--ros-setup", "--driver-setup", "--results-dir"} <= options
        assert not {option for option in options if "_" in option}, test_id
        assert not options.intersection(REMOVED_OPTIONS), test_id


def test_lifecycle_and_camera_options_match_script_capabilities():
    for test_id, script in SCRIPTS.items():
        output = script_help(script)
        assert "--duration" in output
        if test_id == "image_receive_stats":
            assert "--run-count" not in output
            assert "--camera" not in output
            assert "--image-topic" in output
        else:
            assert "--run-count" in output
            assert "--camera" in output


def test_invalid_cli_returns_argparse_exit_code():
    script = SCRIPTS["launch_restart"]
    completed = subprocess.run(
        [sys.executable, str(script), "--unknown-option"],
        cwd=str(script.parent),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 2


def test_preset_launch_log_match_survives_recent_line_eviction(tmp_path):
    module = load_script(SCRIPTS["preset_upgrade"])
    expected = "Loaded device preset: Forklift"
    lines = [expected, *(f"DEBUG startup line {index}" for index in range(600))]
    log_file = tmp_path / "camera.launch.log"
    session = module.LaunchSession(
        camera_name="camera",
        command=[],
        work_dir=tmp_path,
        env={},
        log_file=log_file,
        emit=lambda _message: None,
    )

    session._lines.extend(lines)

    assert len(session._lines) == 300
    assert not any(expected in line for line in session._lines)
    assert not session.has_log_substring(expected)

    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert session.has_log_substring(expected)


def test_camera_launch_stress_defaults_enable_heartbeat_and_firmware_log():
    expressions = {
        "launch_restart": (
            'module.merge_launch_arg_overrides(module.DEFAULT_STRESS_LAUNCH_ARGS, '
            '["enable_heartbeat=false"])'
        ),
        "launch_param_load": (
            'module.merge_launch_arg_overrides(module.DEFAULT_STRESS_LAUNCH_ARGS, '
            '["enable_heartbeat=false"])'
        ),
        "export_load": 'module.build_common_launch_args(["enable_heartbeat=false"])',
        "preset_upgrade": (
            'module.build_base_launch_args(type("Args", (), '
            '{"launch_arg": ["enable_heartbeat=false"]})())'
        ),
        "stream_toggle": (
            'module.merge_launch_arg_overrides(module.DEFAULT_STRESS_LAUNCH_ARGS, '
            '["enable_heartbeat=false"])'
        ),
    }
    for test_id, expression in expressions.items():
        launch_args = evaluate_script(SCRIPTS[test_id], expression)
        assert launch_args["enable_heartbeat"] == "false", test_id
        assert launch_args["enable_firmware_log"] == "true", test_id


def test_image_saving_uses_stream_directories_and_continuing_indices(tmp_path):
    topics_and_directories = {
        "/camera/color/image_raw": "color",
        "/camera/depth/image_raw": "depth",
        "/camera/ir/image_raw": "ir",
        "/camera/left_ir/image_raw": "ir_left",
        "/camera/right_ir/image_raw": "ir_right",
        "/camera/left_color/image_raw": "color_left",
        "/camera/right_color/image_raw": "color_right",
    }
    for test_id in ("export_load", "preset_upgrade", "launch_param_load"):
        module = load_script(SCRIPTS[test_id])
        output_root = tmp_path / test_id / "images"
        for topic, directory in topics_and_directories.items():
            assert module.image_stream_name(topic) == directory

        existing = output_root / "camera_01" / "depth" / "image_0003.jpg"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"existing")
        sequence = module.ImagePathSequence(output_root)
        first = sequence.next_path("/camera_01/depth/image_raw", "camera_01")
        second = sequence.next_path("/camera_01/depth/image_raw", "camera_01")
        color = sequence.next_path("/camera_01/color/image_raw", "camera_01")
        other_camera = sequence.next_path("/camera_02/depth/image_raw", "camera_02")

        assert first == output_root / "camera_01" / "depth" / "image_0004.png"
        assert second == output_root / "camera_01" / "depth" / "image_0005.png"
        assert color == output_root / "camera_01" / "color" / "image_0001.png"
        assert other_camera == output_root / "camera_02" / "depth" / "image_0001.png"
        compressed = sequence.next_path(
            "/camera_01/color/image_raw/compressed", "camera_01", ".jpg"
        )
        assert compressed == output_root / "camera_01" / "color" / "image_0002.jpg"


def test_all_image_savers_preserve_uint16_png_and_compressed_bytes(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    pixels = np.array([[0, 1, 1024], [4096, 32768, 65535]], dtype=np.uint16)
    raw_message = SimpleNamespace(
        width=3,
        height=2,
        encoding="16UC1",
        step=6,
        is_bigendian=0,
        data=pixels.tobytes(),
    )
    compressed_message = SimpleNamespace(data=b"not-validated-compressed-payload")

    class FakeBridge:
        def imgmsg_to_cv2(self, _message, desired_encoding):
            assert desired_encoding == "passthrough"
            return pixels.copy()

    for test_id in ("export_load", "preset_upgrade", "launch_param_load"):
        module = load_script(SCRIPTS[test_id])
        topic = "/camera_01/depth/image_raw"
        raw_path = tmp_path / test_id / "raw.png"
        compressed_path = tmp_path / test_id / "compressed.jpg"

        if test_id == "export_load":
            saver = object.__new__(module.ImageSaver)
            saver.metadata = {topic: {"topic_kind": "raw"}}
            saver._bridge = FakeBridge()
            saver._cv2 = cv2
            saver._write_image(topic, raw_message, raw_path)
            saver.metadata[topic]["topic_kind"] = "compressed"
            saver._write_image(topic, compressed_message, compressed_path)
        else:
            saver = object.__new__(module.ImageCaptureMonitor)
            saver.state = {topic: {"topic_kind": "raw"}}
            saver._bridge = FakeBridge()
            saver._cv2 = cv2
            saver._write_image(topic, raw_message, raw_path)
            saver.state[topic]["topic_kind"] = "compressed"
            saver._write_image(topic, compressed_message, compressed_path)

        decoded = cv2.imread(str(raw_path), cv2.IMREAD_UNCHANGED)
        assert decoded.dtype == np.uint16
        assert np.array_equal(decoded, pixels)
        assert compressed_path.read_bytes() == compressed_message.data

    module = load_script(SCRIPTS["stream_toggle"])
    writer = module.ImageWriter(tmp_path / "stream_toggle")
    writer.bridge = FakeBridge()
    writer.cv2 = cv2
    raw_target = module.save_image_target_from_topic(
        "/camera_01/depth/image_raw"
    )
    compressed_target = module.save_image_target_from_topic(
        "/camera_01/depth/image_raw/compressed"
    )
    raw_record = writer.write(raw_target, raw_message)
    compressed_record = writer.write(compressed_target, compressed_message)
    decoded = cv2.imread(raw_record["path"], cv2.IMREAD_UNCHANGED)
    assert decoded.dtype == np.uint16
    assert np.array_equal(decoded, pixels)
    assert Path(compressed_record["path"]).read_bytes() == compressed_message.data


def test_default_image_discovery_finds_all_raw_streams_per_camera():
    topic_types = {
        "/camera_01/color/image_raw": ["sensor_msgs/msg/Image"],
        "/camera_01/depth/image_raw": ["sensor_msgs/msg/Image"],
        "/camera_01/left_ir/image_raw": ["sensor_msgs/msg/Image"],
        "/camera_02/right_ir/image_raw": ["sensor_msgs/Image"],
        "/camera_01/color/image_raw/compressed": ["sensor_msgs/msg/CompressedImage"],
        "/unrelated/image_raw": ["sensor_msgs/msg/Image"],
    }

    class FakeHarness:
        def get_topic_names_and_types(self):
            return topic_types

        def spin_once(self, _timeout):
            pass

    class FakeSession:
        def assert_running(self):
            pass

    expected_topics = [
        "/camera_01/color/image_raw",
        "/camera_01/depth/image_raw",
        "/camera_01/left_ir/image_raw",
        "/camera_02/right_ir/image_raw",
    ]
    for test_id in ("export_load", "preset_upgrade"):
        module = load_script(SCRIPTS[test_id])
        topics, topic_cameras = module.discover_image_topics(
            harness=FakeHarness(),
            camera_names=["camera_01", "camera_02"],
            sessions=[FakeSession(), FakeSession()],
            timeout=0.1,
            settle_seconds=0.0,
        )
        assert topics == expected_topics
        assert topic_cameras["/camera_01/left_ir/image_raw"] == "camera_01"
        assert topic_cameras["/camera_02/right_ir/image_raw"] == "camera_02"

    module = load_script(SCRIPTS["launch_param_load"])
    assert module.discover_image_topics(
        harness=FakeHarness(),
        camera_name="camera_01",
        timeout=0.1,
        settle_seconds=0.0,
    ) == expected_topics[:3]


def test_explicit_compressed_topics_resolve_to_compressed_message_type():
    topic_types = {
        "/camera/color/image_raw": ["sensor_msgs/msg/Image"],
        "/camera/color/image_raw/compressed": [
            "sensor_msgs/msg/CompressedImage"
        ],
    }
    for test_id in ("export_load", "preset_upgrade", "launch_param_load"):
        module = load_script(SCRIPTS[test_id])
        harness_type = (
            module.RosHarness if test_id == "export_load" else module.RosImageHarness
        )
        harness = object.__new__(harness_type)
        harness.get_topic_names_and_types = lambda: topic_types

        assert harness.resolve_image_topic_kind(
            "/camera/color/image_raw"
        ) == "raw"
        assert harness.resolve_image_topic_kind(
            "/camera/color/image_raw/compressed"
        ) == "compressed"
