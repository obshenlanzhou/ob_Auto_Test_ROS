from pathlib import Path
import json
import os
import re
import signal
import subprocess
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
STANDALONE_ROOT = REPOSITORY_ROOT / "standalone_test_scripts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(
    0,
    str(REPOSITORY_ROOT / "auto_test_ws" / "src" / "orbbec_camera_auto_test"),
)

import orbbec_camera_auto_test_ui.server as ui_server  # noqa: E402
from orbbec_camera_auto_test_ui.standalone import (  # noqa: E402
    REQUIRED_RESULT_KEYS,
    build_command,
    load_manifests,
    manifest_catalog,
    validate_request,
    validate_result_contract,
)
from orbbec_camera_auto_test_ui.run_manager import (  # noqa: E402
    RunManager,
    TestJob as _TestJob,
)


def test_all_seven_standalone_manifests_load():
    manifests = load_manifests(STANDALONE_ROOT)
    assert len(manifests) == 7
    assert {item["id"] for item in manifests} == {
        "export_load_stress_test",
        "firmware_update_stress_test",
        "image_receive_stats_test",
        "launch_param_load_stress",
        "launch_restart_stream_check",
        "preset_upgrade_stress_test",
        "stream_toggle_stress_test",
    }
    manifests_with_launch_args = {
        manifest["id"]
        for manifest in manifests
        if any(field["option"] == "--launch-arg" for field in manifest["fields"])
    }
    assert manifests_with_launch_args == {
        "export_load_stress_test",
        "launch_param_load_stress",
        "launch_restart_stream_check",
        "preset_upgrade_stress_test",
        "stream_toggle_stress_test",
    }
    for manifest in manifests:
        for field in manifest["fields"]:
            assert field["group"] in {
                "environment",
                "configuration",
                "cameras",
                "limits",
                "advanced",
            }
            assert field["label_en"]


def test_manifest_options_exist_in_script_help():
    for manifest in load_manifests(STANDALONE_ROOT):
        commands = [[sys.executable, manifest["script_path"], "--help"]]
        if manifest["id"] == "image_receive_stats_test":
            commands.append(
                [sys.executable, manifest["script_path"], "--ros-version", "1", "--help"]
            )
        output = ""
        for command in commands:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert completed.returncode == 0
            output += completed.stdout
        options = set(re.findall(r"(?<!\w)--[a-z0-9-]+", output))
        assert {field["option"] for field in manifest["fields"]} <= options


def test_camera_fields_build_launch_style_specs(tmp_path):
    manifest = manifest_catalog(STANDALONE_ROOT)["launch_restart_stream_check"]
    args, values = build_command(
        manifest,
        {
            "ros_version": "2",
            "cameras": [
                {
                    "name": "camera_01",
                    "serial-number": "SN001",
                    "usb-port": "2-1",
                    "device-ip": "",
                    "device-port": "",
                    "config-file-path": "",
                }
            ],
        },
        tmp_path,
    )
    camera_index = args.index("--camera")
    assert args[camera_index + 1] == "name=camera_01,serial-number=SN001,usb-port=2-1"
    assert values["cameras"][0]["serial-number"] == "SN001"
    assert args[-2:] == ["--results-dir", str(tmp_path)]


def test_ros_specific_image_fields_are_conditional(tmp_path):
    manifest = manifest_catalog(STANDALONE_ROOT)["image_receive_stats_test"]
    base = {"image_topics": ["/camera/color/image_raw"]}

    ros1, _ = build_command(manifest, {**base, "ros_version": "1"}, tmp_path)
    ros2, _ = build_command(manifest, {**base, "ros_version": "2"}, tmp_path)

    assert "--buff-size" in ros1
    assert "--qos" not in ros1
    assert "--qos" in ros2
    assert "--buff-size" not in ros2


def test_launch_param_manifest_requires_camera_config_file():
    manifest = manifest_catalog(STANDALONE_ROOT)["launch_param_load_stress"]
    _values, errors = validate_request(
        manifest,
        {
            "launch_file": "camera.launch.py",
            "cameras": [{"name": "camera"}],
        },
    )
    assert any("config-file-path" in error for error in errors)


def test_result_contract_rejects_missing_and_accepts_complete_result(tmp_path):
    result_path = tmp_path / "result.json"
    _payload, errors = validate_result_contract(result_path, "example")
    assert errors == ["result.json is missing"]

    payload = {key: None for key in REQUIRED_RESULT_KEYS}
    payload.update(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "test_id": "example",
            "status": "passed",
            "request": {},
            "environment": {},
            "summary": {},
            "warnings": [],
            "details": {},
            "artifacts": [],
        }
    )
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    persisted, errors = validate_result_contract(result_path, "example")
    assert not errors
    assert persisted["status"] == "passed"


def test_high_risk_run_requires_server_side_confirmation():
    manager = RunManager()
    status, payload = manager.start_standalone(
        {
            "test_id": "firmware_update_stress_test",
            "values": {"firmwares": ["/tmp/fw.bin"]},
        }
    )
    assert status == 400
    assert payload["errors"] == ["high-risk confirmation is required"]


def test_run_manager_requires_valid_standalone_result(tmp_path):
    manager = RunManager()
    job = _TestJob(
        run_id="run-1",
        mode="standalone:example",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
        runner_type="standalone",
        test_id="example",
    )
    manager._run_job(job, "true")
    assert job.status == "failed"
    assert job.done_event.is_set()
    assert any("result.json is missing" in line for line in job.logs)


def test_run_manager_accepts_valid_standalone_result(tmp_path):
    payload = {key: None for key in REQUIRED_RESULT_KEYS}
    payload.update(
        {
            "schema_version": 1,
            "run_id": "script-run-1",
            "test_id": "example",
            "status": "passed",
            "request": {},
            "environment": {},
            "summary": {},
            "warnings": [],
            "details": {},
            "artifacts": [],
        }
    )
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    manager = RunManager()
    job = _TestJob(
        run_id="run-1",
        mode="standalone:example",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
        runner_type="standalone",
        test_id="example",
    )
    manager._run_job(job, "true")
    assert job.status == "passed"
    assert job.exit_code == 0


class _FakeProcess:
    def __init__(self, pid=1234):
        self.pid = pid
        self.signals = []

    def poll(self):
        return None

    def send_signal(self, value):
        self.signals.append(value)


class _ImmediateEvent:
    def __init__(self, result=False):
        self.result = result
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.result

    def set(self):
        self.result = True


def test_stop_requested_during_start_is_delivered_after_process_is_created(tmp_path):
    manager = RunManager()
    job = _TestJob(
        run_id="run-starting",
        mode="functional",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
    )
    manager._current = job

    snapshot = manager.stop()
    assert snapshot["status"] == "stopping"
    assert job.stop_requested is True
    assert job.stop_signal_sent is False

    process = _FakeProcess()
    job.process = process
    manager._send_requested_stop(job)
    assert job.stop_signal_sent is True


def test_safe_point_stop_signals_only_the_script_process(tmp_path, monkeypatch):
    manager = RunManager()
    process = _FakeProcess()
    job = _TestJob(
        run_id="safe-run",
        mode="standalone:upgrade",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
        process=process,
        status="running",
        stop_policy="safe-point",
    )
    manager._current = job
    group_signals = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: group_signals.append((pid, sig)))

    manager.stop()

    assert process.signals == [signal.SIGINT]
    assert group_signals == []


def test_shutdown_escalates_an_unresponsive_immediate_process_group(tmp_path, monkeypatch):
    manager = RunManager()
    process = _FakeProcess()
    event = _ImmediateEvent(result=False)
    job = _TestJob(
        run_id="hung-run",
        mode="functional",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
        process=process,
        status="running",
        done_event=event,
    )
    manager._current = job
    signals = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    assert manager.shutdown(timeout=0) is False
    assert [item[1] for item in signals] == [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGKILL,
    ]


def test_standalone_snapshot_reports_initial_and_current_round(tmp_path):
    job = _TestJob(
        run_id="run-1",
        mode="standalone:example",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
        runner_type="standalone",
        test_id="example",
        standalone_rounds_supported=True,
        standalone_round_total=10,
    )

    initial = job.snapshot()
    assert initial["standalone"]["progress"] == {
        "supported": True,
        "current": 0,
        "total": 10,
    }

    events = [
        {"event": "phase", "phase": "starting"},
        {"event": "progress", "current": 3, "total": 10, "phase": "running"},
        {"event": "log", "message": "round still running"},
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    current = job.snapshot()
    assert current["standalone"]["progress"] == {
        "supported": True,
        "current": 3,
        "total": 10,
    }
    assert set(current["performance"]) == {"elapsed_seconds"}
    assert current["restart"] == {"available": False}


def test_standalone_snapshot_marks_rounds_not_applicable(tmp_path):
    job = _TestJob(
        run_id="run-1",
        mode="standalone:image_receive_stats_test",
        run_root=tmp_path,
        command_lines=[],
        shell="bash",
        runner_type="standalone",
        test_id="image_receive_stats_test",
    )

    snapshot = job.snapshot()
    assert snapshot["standalone"]["progress"] == {
        "supported": False,
        "current": None,
        "total": None,
    }


def test_standalone_monitor_template_contains_only_compact_metrics():
    template = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="standaloneElapsed"' in template
    assert 'id="standaloneRound"' in template
    assert 'id="standaloneEventList"' not in template
    assert 'id="standaloneProgressStatus"' not in template


def test_standalone_ros1_defaults_switch_setup_driver_and_launch(monkeypatch):
    monkeypatch.setattr(
        ui_server,
        "setup_defaults",
        lambda: {
            "2": {
                "ros_setup": "/opt/ros/humble/setup.bash",
                "driver_setup": "/driver/ros2/install/setup.bash",
            },
            "1": {
                "ros_setup": "/opt/ros/one/setup.bash",
                "driver_setup": "/driver/ros1/devel/setup.bash",
            },
        },
    )
    monkeypatch.setattr(
        ui_server,
        "load_config",
        lambda: {
            "standalone_configs": {
                "export_load_stress_test": {
                    "ros_version": "1",
                    "ros_setup": "/opt/ros/humble/setup.bash",
                    "driver_setup": "",
                    "launch_file": "gemini_330_series_sdk_json.launch.py",
                }
            }
        },
    )

    payload = ui_server.list_standalone_tests()
    test = next(
        item
        for item in payload["tests"]
        if item["id"] == "export_load_stress_test"
    )

    assert test["values"]["ros_setup"] == "/opt/ros/one/setup.bash"
    assert test["values"]["driver_setup"] == "/driver/ros1/devel/setup.bash"
    assert test["values"]["launch_file"] == "gemini_330_series_sdk_json.launch"


def test_standalone_ros_change_handler_updates_version_dependent_fields():
    script = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "function updateStandaloneRosVersion(rosVersion)" in script
    assert "updateStandaloneRosVersion(control.value)" in script
    assert "launchFileForRosVersion(launchFile.value, rosVersion)" in script
    assert "launchFileForRosVersion(launchFile.placeholder, rosVersion)" in script


def test_camera_editor_separates_usb_and_network_fields():
    script = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'usb: ["name", "serial-number", "usb-port"]' in script
    assert 'network: ["name", "device-ip", "device-port"]' in script
    assert 'if (field.config_file_required) cameraFields.push("config-file-path")' in script
    assert 'rowHeader.className = "camera-row-header"' in script
    assert 'headingTitle.textContent = kind === "usb" ? "USB 相机" : "网络相机"' in script
    assert '["", "＋ 添加相机"]' in script
    assert '["usb", "USB 相机"]' in script
    assert '["network", "网络相机"]' in script
    assert "rowHeader.append(heading, remove)" in script
    assert "groupHeading.insertBefore(cameraActions, group.status)" in script


def test_launch_restart_camera_editor_enforces_single_camera_with_multi_launch_hint():
    manifest = manifest_catalog(STANDALONE_ROOT)["launch_restart_stream_check"]
    cameras = next(
        field for field in manifest["fields"] if field["name"] == "cameras"
    )
    assert cameras["max_items"] == 1
    assert cameras["note"] == (
        "仅支持添加一台相机；多相机压测请配置并选择多相机启动 Launch 文件。"
    )

    script = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert 'header.classList.toggle("is-hidden", reachedMaximum)' in script
    assert "addSelect.disabled = reachedMaximum" in script
    assert "addCameraRow(editor, kind, camera, field, syncCameraAddActions)" in script
    assert 'note.className = "field-note camera-limit-note"' in script


def test_standalone_ui_renders_grouped_sections_and_completion_statuses():
    template = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")
    script = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'id="standaloneFieldGroups"' in template
    for group in ("environment", "configuration", "cameras", "limits", "advanced"):
        assert f'id: "{group}"' in script
    assert "function createStandaloneGroup(definition)" in script
    assert "function updateStandaloneGroupStatuses(errors)" in script
    assert 'status.textContent = complete ? "已完成"' in script
    assert "满足任一条件即结束；留空表示不限制。" in script


def test_standalone_paths_use_full_width_and_bilingual_labels():
    script = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    stylesheet = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "style.css"
    ).read_text(encoding="utf-8")

    assert 'wrapper.classList.add("standalone-path-field", "grid-span-2")' in script
    assert "function createFieldLabel(primaryText, secondaryText" in script
    assert ".standalone-path-field" in stylesheet
    assert ".path-input" in stylesheet


def test_multi_value_fields_show_at_least_two_examples():
    for manifest in load_manifests(STANDALONE_ROOT):
        for field in manifest["fields"]:
            if field["type"] != "list":
                continue
            examples = [
                line.strip()
                for line in str(field.get("placeholder") or "").splitlines()
                if line.strip()
            ]
            assert len(examples) >= 2, (manifest["id"], field["name"])

    template = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'placeholder="enable_point_cloud=true&#10;enable_colored_point_cloud=true"' in template
    assert 'placeholder="/{camera}/color/image_raw&#10;/{camera}/depth/image_raw"' in template


def test_camera_launch_stress_manifests_enable_diagnostics_by_default():
    manifests = manifest_catalog(STANDALONE_ROOT)
    for test_id in (
        "launch_restart_stream_check",
        "launch_param_load_stress",
        "export_load_stress_test",
        "preset_upgrade_stress_test",
    ):
        launch_args = next(
            field
            for field in manifests[test_id]["fields"]
            if field["name"] == "launch_args"
        )
        assert launch_args["default"] == [
            "enable_heartbeat=true",
            "enable_firmware_log=true",
        ]


def test_dynamic_fields_use_direct_placeholders_without_example_prefix():
    script = (
        PACKAGE_ROOT
        / "orbbec_camera_auto_test_ui"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'control.placeholder = field.placeholder || "示例值 1\\n示例值 2"' in script
    assert '"serial-number": "CV2R1610002F"' in script
    assert '"device-ip": "192.168.1.10"' in script
    assert "例如：" not in script
