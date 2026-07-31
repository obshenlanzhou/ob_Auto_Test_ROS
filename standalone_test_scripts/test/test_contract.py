from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


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
}


def load_protocol(path: Path):
    module_name = f"test_protocol_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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


def test_protocol_copies_are_identical():
    expected = PROTOCOLS[0].read_bytes()
    assert all(path.read_bytes() == expected for path in PROTOCOLS)


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
    }
    for test_id, expression in expressions.items():
        launch_args = evaluate_script(SCRIPTS[test_id], expression)
        assert launch_args["enable_heartbeat"] == "false", test_id
        assert launch_args["enable_firmware_log"] == "true", test_id
