from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from orbbec_camera_auto_test_ui.device_info import (  # noqa: E402
    DeviceQueryError,
    parse_device_output,
    query_camera_devices,
)


SAMPLE_OUTPUT = """\
[INFO] [1785398761.281584036] [list_device_node]: name: Orbbec Gemini 305
[INFO] [1785398761.281630551] [list_device_node]: pid: 0x0840
[INFO] [1785398761.281641197] [list_device_node]: serial: CV2R1610002F
[INFO] [1785398761.281649606] [list_device_node]: connection: USB3.2
[INFO] [1785398761.281657044] [list_device_node]: firmware version: 1.0.85
[INFO] [1785398761.281663925] [list_device_node]: usb port: 2-1
[INFO] [1785398761.281677461] [list_device_node]: device_preset count: 2
[INFO] [1785398761.281684933] [list_device_node]:   - Default
[INFO] [1785398761.281691597] [list_device_node]:   - High Accuracy
[INFO] [1785398761.282952326] [list_device_node]: color_preset count: 1
[INFO] [1785398761.282960289] [list_device_node]:   - Warm Biased AWB
[INFO] [1785398761.282983597] [list_device_node]: preset version: 0.0.9

[INFO] [1785398761.534200230] [list_device_node]: name: Orbbec Gemini 336L
[INFO] [1785398761.534213831] [list_device_node]: pid: 0x0807
[INFO] [1785398761.534217017] [list_device_node]: serial: CP82841000M3
[INFO] [1785398761.534219740] [list_device_node]: connection: USB3.2
[INFO] [1785398761.534222148] [list_device_node]: firmware version: 1.8.00
[INFO] [1785398761.534224472] [list_device_node]: usb port: 2-4
[INFO] [1785398761.534228175] [list_device_node]: device_preset count: 1
[INFO] [1785398761.534231407] [list_device_node]:   - Factory Calib
[INFO] [1785398761.534242594] [list_device_node]: color_preset count: 1
[INFO] [1785398761.534245307] [list_device_node]:   - Default
[INFO] [1785398761.534250409] [list_device_node]: preset version: 3.0.7
"""


def test_parse_device_output_supports_multiple_cameras_and_presets():
    devices = parse_device_output(SAMPLE_OUTPUT)

    assert len(devices) == 2
    assert devices[0] == {
        "name": "Orbbec Gemini 305",
        "pid": "0x0840",
        "serial": "CV2R1610002F",
        "connection": "USB3.2",
        "firmware_version": "1.0.85",
        "usb_port": "2-1",
        "device_presets": ["Default", "High Accuracy"],
        "color_presets": ["Warm Biased AWB"],
        "preset_version": "0.0.9",
    }
    assert devices[1]["name"] == "Orbbec Gemini 336L"
    assert devices[1]["device_presets"] == ["Factory Calib"]
    assert devices[1]["preset_version"] == "3.0.7"


def test_query_camera_devices_sources_setups_and_returns_parsed_data(tmp_path):
    ros_setup = tmp_path / "ros setup.bash"
    camera_setup = tmp_path / "camera setup.bash"
    ros_setup.touch()
    camera_setup.touch()
    completed = SimpleNamespace(returncode=0, stdout=SAMPLE_OUTPUT)

    with patch(
        "orbbec_camera_auto_test_ui.device_info.subprocess.run",
        return_value=completed,
    ) as run:
        payload = query_camera_devices(
            {
                "ros_version": "2",
                "ros_domain_id": "18",
                "ros_setup": str(ros_setup),
                "camera_setup": str(camera_setup),
            }
        )

    assert payload["count"] == 2
    assert payload["devices"][0]["serial"] == "CV2R1610002F"
    command = run.call_args.args[0]
    assert command[:2] == ["bash", "-lc"]
    assert f"source '{ros_setup}'" in command[2]
    assert f"source '{camera_setup}'" in command[2]
    assert "export ROS_DOMAIN_ID=18" in command[2]
    assert "ros2 run orbbec_camera list_devices_node" in command[2]


def test_query_camera_devices_rejects_ros1_before_running_command():
    with patch("orbbec_camera_auto_test_ui.device_info.subprocess.run") as run:
        with pytest.raises(DeviceQueryError, match="only available for ROS 2") as error:
            query_camera_devices({"ros_version": "1"})

    assert error.value.status == 400
    run.assert_not_called()


def test_query_camera_devices_unsets_inherited_domain_when_value_is_empty(tmp_path):
    ros_setup = tmp_path / "setup.bash"
    ros_setup.touch()
    completed = SimpleNamespace(returncode=0, stdout="")

    with patch(
        "orbbec_camera_auto_test_ui.device_info.subprocess.run",
        return_value=completed,
    ) as run:
        query_camera_devices(
            {"ros_version": "2", "ros_domain_id": "", "ros_setup": str(ros_setup)}
        )

    assert "unset ROS_DOMAIN_ID" in run.call_args.args[0][2]


def test_query_camera_devices_exposes_command_failure_output(tmp_path):
    ros_setup = tmp_path / "setup.bash"
    ros_setup.touch()
    completed = SimpleNamespace(returncode=7, stdout="device permission denied")

    with patch(
        "orbbec_camera_auto_test_ui.device_info.subprocess.run",
        return_value=completed,
    ):
        with pytest.raises(DeviceQueryError, match="exited with code 7") as error:
            query_camera_devices(
                {"ros_version": "2", "ros_setup": str(ros_setup), "camera_setup": ""}
            )

    assert error.value.status == 502
    assert error.value.output == "device permission denied"
