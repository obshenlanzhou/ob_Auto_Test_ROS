from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

import orbbec_camera_auto_test_ui.run_manager as run_manager  # noqa: E402
from orbbec_camera_auto_test_ui.run_manager import (  # noqa: E402
    _build_shell_script,
    _ros_domain_environment_command,
    _build_runner_args,
    build_performance_metrics,
    normalize_ros_domain_id,
    validate_run_payload,
)


class RunConfigurationTest(unittest.TestCase):
    def base_payload(self):
        return {
            "mode": "performance",
            "ros_version": "2",
            "ros_setup": "/bin/sh",
            "launch_file": "gemini_301_series.launch.py",
            "launch_config": "generic",
            "performance_scenario": "drop_frame",
            "stream_options": {"enable_color": "true", "enable_depth": ""},
        }

    def test_generic_stream_options_become_launch_args(self):
        args = _build_runner_args(self.base_payload(), "performance", Path("/tmp/results"))
        self.assertIn("--scenario", args)
        self.assertIn("drop_frame", args)
        self.assertIn("enable_color=true", args)
        self.assertNotIn("--profile", args)

    def test_dual_color_uses_driver_config_and_ignores_stream_options(self):
        payload = self.base_payload()
        payload["launch_config"] = "dual_color"
        args = _build_runner_args(payload, "performance", Path("/tmp/results"))
        config_index = args.index("--config-file-path")
        self.assertEqual(args[config_index + 1], "gemini305_dual_color.yaml")
        self.assertIn("device_preset=Dual Color Streams", args)
        self.assertNotIn("enable_color=true", args)

    def test_special_config_is_tied_to_its_launch(self):
        payload = self.base_payload()
        payload["launch_file"] = "gemini2L.launch.py"
        payload["launch_config"] = "dual_color"
        self.assertTrue(any("not supported" in item for item in validate_run_payload(payload)))

    def test_ros2_domain_id_is_validated_and_exported(self):
        self.assertEqual(normalize_ros_domain_id("007"), "7")
        self.assertEqual(
            _ros_domain_environment_command("2", "7"), "export ROS_DOMAIN_ID=7"
        )
        self.assertEqual(
            _ros_domain_environment_command("2", ""), "unset ROS_DOMAIN_ID"
        )
        self.assertEqual(_ros_domain_environment_command("1", "7"), "")

        payload = self.base_payload()
        payload["ros_domain_id"] = "42"
        script, _commands, _shell = _build_shell_script(
            payload, Path("/tmp/domain-results")
        )
        self.assertIn("export ROS_DOMAIN_ID=42", script)
        self.assertIn("[UI] ROS_DOMAIN_ID=42", script)

    def test_ros_domain_id_rejects_out_of_range_and_non_integer_values(self):
        for value in ("-1", "1.5", "233", "domain"):
            payload = self.base_payload()
            payload["ros_domain_id"] = value
            self.assertTrue(
                any("Domain ID" in item for item in validate_run_payload(payload)),
                value,
            )

    def test_ros_domain_id_config_does_not_inherit_environment_and_persists_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ui_config.json"
            with patch.object(run_manager, "CONFIG_PATH", config_path), patch.dict(
                os.environ, {"ROS_DOMAIN_ID": "19"}
            ):
                self.assertEqual(run_manager.load_config()["ros_domain_id"], "")
                run_manager.save_config({"ros_domain_id": "21"})
                self.assertEqual(run_manager.load_config()["ros_domain_id"], "21")
                run_manager.save_config({"ros_domain_id": ""})
                self.assertEqual(run_manager.load_config()["ros_domain_id"], "")

    def test_framework_modes_keep_independent_configuration_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ui_config.json"
            with patch.object(run_manager, "CONFIG_PATH", config_path):
                run_manager.save_config(
                    {
                        "mode": "functional",
                        "launch_file": "astra2.launch.py",
                        "run_count": "2",
                        "stream_options": {"enable_color": "true"},
                        "camera_name": "functional_camera",
                        "launch_args": "enable_ir=true",
                    }
                )
                run_manager.save_config(
                    {
                        "mode": "restart",
                        "launch_file": "gemini_330_series.launch.py",
                        "run_count": "8",
                        "duration": "30m",
                        "stream_options": {"enable_depth": "true"},
                        "camera_name": "restart_camera",
                        "launch_args": "enable_ir=false",
                    }
                )

                config = run_manager.load_config()

            self.assertEqual(
                config["mode_configs"]["functional"]["launch_file"],
                "astra2.launch.py",
            )
            self.assertEqual(config["mode_configs"]["functional"]["run_count"], "2")
            self.assertEqual(
                config["mode_configs"]["functional"]["stream_options"],
                {"enable_color": "true"},
            )
            self.assertEqual(
                config["mode_configs"]["functional"]["camera_name"],
                "functional_camera",
            )
            self.assertEqual(
                config["mode_configs"]["functional"]["launch_args"],
                "enable_ir=true",
            )
            self.assertEqual(
                config["mode_configs"]["restart"]["launch_file"],
                "gemini_330_series.launch.py",
            )
            self.assertEqual(config["mode_configs"]["restart"]["run_count"], "8")
            self.assertEqual(config["mode_configs"]["restart"]["duration"], "30m")
            self.assertEqual(
                config["mode_configs"]["restart"]["stream_options"],
                {"enable_depth": "true"},
            )
            self.assertEqual(
                config["mode_configs"]["restart"]["camera_name"],
                "restart_camera",
            )
            self.assertEqual(
                config["mode_configs"]["restart"]["launch_args"],
                "enable_ir=false",
            )

    def test_point_cloud_does_not_reuse_depth_image_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            (run_root / "launch.log").write_text(
                "[camera.camera]: depth Frame - Width: 848 Height: 480 fps: 30 Format: Y16\n",
                encoding="utf-8",
            )
            (run_root / "fps.csv").write_text(
                "elapsed_seconds,camera_depth_points_ideal_fps,"
                "camera_depth_points_current_fps,camera_depth_points_avg_fps,"
                "camera_depth_points_dropped_frames,camera_depth_points_drop_rate\n"
                "1.0,30,29.96,29.96,0,0\n",
                encoding="utf-8",
            )

            metrics = build_performance_metrics(run_root)

        self.assertEqual(len(metrics["fps_topics"]), 1)
        point_cloud = metrics["fps_topics"][0]
        self.assertEqual(point_cloud["topic"], "/camera/depth/points")
        self.assertEqual(point_cloud["resolution"], "")
        self.assertEqual(point_cloud["stream_format"], "XYZ")
        self.assertEqual(point_cloud["ideal_fps"], 30.0)


if __name__ == "__main__":
    unittest.main()
