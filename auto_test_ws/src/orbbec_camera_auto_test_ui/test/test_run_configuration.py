from pathlib import Path
import sys
import tempfile
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from orbbec_camera_auto_test_ui.run_manager import (  # noqa: E402
    _build_runner_args,
    build_performance_metrics,
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
