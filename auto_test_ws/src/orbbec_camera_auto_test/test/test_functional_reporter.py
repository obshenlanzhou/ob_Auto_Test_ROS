from orbbec_camera_auto_test.core.reporter import (
    build_functional_summary,
    collect_failures,
)


def test_functional_summary_lists_each_executed_check() -> None:
    result = {
        "profile_name": "generic_functional",
        "camera_name": "camera",
        "launch_file": "camera.launch.py",
        "status": "failed",
        "scenarios": [
            {
                "name": "default",
                "status": "failed",
                "message": "",
                "topics": [
                    {
                        "name": "/camera/color/image_raw",
                        "type": "sensor_msgs/msg/Image",
                        "mode": "message",
                        "validator": "image",
                        "status": "passed",
                        "message": "",
                        "metrics": {"width": 1280, "height": 720},
                    }
                ],
                "services": [
                    {
                        "name": "/camera/get_sdk_version",
                        "type": "orbbec_camera_msgs/srv/GetString",
                        "mode": "read",
                        "status": "failed",
                        "message": "response.success is not true",
                    }
                ],
                "artifacts": [
                    {
                        "name": "/camera/save_images",
                        "type": "std_srvs/srv/Empty",
                        "mode": "artifact",
                        "status": "passed",
                        "message": "created 2 files",
                        "new_files": ["image/color.png", "image/depth.png"],
                    }
                ],
                "reboot": {
                    "name": "/camera/reboot_device",
                    "type": "std_srvs/srv/Empty",
                    "status": "passed",
                    "message": "reboot and image stream recovery succeeded",
                },
            }
        ],
    }

    summary = "\n".join(build_functional_summary(result))

    assert "## Scenario Details: default" in summary
    assert "| Topic | Type | Check | Status | Details |" in summary
    assert "message (image)" in summary
    assert "/camera/color/image_raw" in summary
    assert '{"height": 720, "width": 1280}' in summary
    assert "| Service | Type | Check | Status | Details |" in summary
    assert "/camera/get_sdk_version" in summary
    assert "response.success is not true" in summary
    assert (
        "| Service | Type | Check | Status | Details | New Files |" in summary
    )
    assert "/camera/save_images" in summary
    assert "image/color.png" in summary
    assert "### Reboot Recovery" in summary
    assert "/camera/reboot_device" in summary


def test_functional_summary_reports_scenario_startup_failure() -> None:
    result = {
        "profile_name": "generic_functional",
        "camera_name": "camera",
        "launch_file": "camera.launch.py",
        "status": "failed",
        "scenarios": [
            {
                "name": "default",
                "status": "failed",
                "message": "camera node did not start",
                "topics": [],
                "services": [],
                "artifacts": [],
                "reboot": {"status": "skipped", "message": "not reached"},
            }
        ],
    }

    summary = "\n".join(build_functional_summary(result))

    assert "### Scenario Result" in summary
    assert "camera node did not start" in summary
    assert "No topic checks were executed" in summary
    assert "No service checks were executed" in summary
    assert collect_failures(result) == ["default: camera node did not start"]
