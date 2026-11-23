from pathlib import Path
import json
import os
import subprocess
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
    _ros_localhost_environment_command,
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

    def test_transport_stream_profiles_are_forwarded_as_launch_arguments(self):
        payload = self.base_payload()
        payload.update(mode="transport_latency", stream_options={
            "color_width": 1280,
            "color_height": "720",
            "color_fps": "30",
            "color_format": "mjpg",
            "depth_width": "01280",
            "depth_height": "720",
            "depth_fps": "",
            "depth_format": "y16",
            "enable_left_ir": "true",
            "left_ir_format": "y8",
            "enable_right_color": "true",
            "right_color_width": "1280",
            "enable_point_cloud": "false",
        })
        self.assertEqual(validate_run_payload(payload), [])
        _, commands, _ = _build_shell_script(payload, Path("/tmp/results"))
        for arg in ("color_width=1280", "color_height=720", "color_fps=30", "color_format=MJPG",
                    "depth_width=1280", "depth_height=720", "depth_format=Y16",
                    "enable_point_cloud=false", "enable_left_ir=true", "left_ir_format=Y8",
                    "enable_right_color=true", "right_color_width=1280"):
            self.assertIn(f"--launch-arg {arg}", commands[0])
        self.assertNotIn("--launch-arg depth_fps=", commands[0])

    def test_invalid_stream_profiles_are_rejected(self):
        payload = self.base_payload()
        payload["stream_options"] = {"depth_width": "0", "color_fps": "29.5", "depth_format": "Y16 Y8"}
        self.assertEqual(validate_run_payload(payload), [
            "depth_width must be a positive integer",
            "color_fps must be a positive integer",
            "depth_format must be a format name such as MJPG or Y16",
        ])

    def test_transport_scenario_routes_selected_mode_to_performance_runner(self):
        payload = self.base_payload()
        payload.update(performance_scenario="transport_latency", transport_mode="intra_process")
        self.assertEqual(validate_run_payload(payload), [])
        args = _build_runner_args(payload, "performance", Path("/tmp/results"))
        self.assertEqual(args[args.index("--transport-mode") + 1], "intra_process")
        payload["ros_version"] = "1"
        self.assertTrue(any("ROS 2" in error for error in validate_run_payload(payload)))

    def test_transport_mode_runs_only_the_selected_transport(self):
        payload = self.base_payload()
        payload.update(
            mode="transport_latency", transport_mode="intra_process", duration="10m",
            transport_reliability="reliable",
            transport_warmup_frames="45", transport_warmup_timeout_sec="90.5",
        )
        self.assertEqual(validate_run_payload(payload), [])
        _script, commands, _shell = _build_shell_script(payload, Path("/tmp/results"))
        self.assertEqual(len(commands), 1)
        self.assertIn("runners.performance", commands[0])
        self.assertIn("--scenario transport_latency", commands[0])
        self.assertIn("--transport-mode intra_process", commands[0])
        self.assertIn("--transport-reliability reliable", commands[0])
        self.assertIn("--transport-warmup-frames 45", commands[0])
        self.assertIn("--transport-warmup-timeout-sec 90.5", commands[0])
        self.assertIn("--duration 10m", commands[0])
        self.assertNotIn("drop_frame", commands[0])
        payload["ros_version"] = "1"
        self.assertTrue(any("ROS 2" in error for error in validate_run_payload(payload)))

    def test_transport_warmup_rejects_invalid_settings(self):
        payload = self.base_payload()
        payload.update(
            mode="transport_latency", transport_warmup_frames="1.5",
            transport_warmup_timeout_sec="nan",
        )
        errors = validate_run_payload(payload)
        self.assertTrue(any(error.startswith("transport_warmup_frames") for error in errors))
        self.assertTrue(any(error.startswith("transport_warmup_timeout_sec") for error in errors))

    def test_transport_rejects_unsupported_reliability(self):
        payload = self.base_payload()
        payload.update(mode="transport_latency", transport_reliability="system_default")
        self.assertIn(
            "transport_reliability must be best_effort or reliable", validate_run_payload(payload)
        )

    def test_gpu_transport_routes_to_driver_benchmark_without_standard_options(self):
        payload = self.base_payload()
        payload.update(
            mode="transport_latency", transport_receiver="gpu", transport_reliability="reliable",
            transport_warmup_timeout_sec="10", transport_warmup_frames="0", duration="15s",
        )
        self.assertEqual(validate_run_payload(payload), [])
        _, commands, _ = _build_shell_script(payload, Path("/tmp/results"))
        self.assertIn("--transport-receiver gpu", commands[0])
        self.assertIn("--transport-warmup-frames 0", commands[0])
        self.assertNotIn("--transport-reliability", commands[0])
        self.assertNotIn("--transport-warmup-timeout-sec", commands[0])
        payload["duration"] = "1.5s"
        self.assertTrue(any("integer" in error for error in validate_run_payload(payload)))

    def test_saved_transport_scenario_migrates_to_its_own_mode(self):
        payload = self.base_payload()
        payload.update(performance_scenario="transport_latency", transport_mode="intra_process")
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "ui_config.json"
            run_manager.write_json(config_path, payload)
            with patch.object(run_manager, "CONFIG_PATH", config_path):
                config = run_manager.load_config()
        self.assertEqual(config["mode"], "transport_latency")
        self.assertEqual(config["mode_configs"]["transport_latency"]["transport_mode"], "intra_process")
        self.assertEqual(config["mode_configs"]["transport_latency"]["transport_reliability"], "best_effort")
        self.assertEqual(config["mode_configs"]["performance"]["performance_scenario"], "")

    def test_transport_metrics_replace_progress_with_final_latency(self):
        header = ("elapsed_seconds,topic,samples,current_fps,avg_fps,"
                  "latency_avg_ms,latency_p95_ms,invalid_stamps,delivery_mismatches\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transport_progress.csv").write_text(
                header + "1,/camera/depth_registered/points,30,30,30,0,,0,0\n"
                + "2,/camera/depth_registered/points,60,30,30,0,,0,0"
            )
            progress = build_performance_metrics(root)
            self.assertTrue(progress["transport"])
            self.assertFalse(progress["transport_final"])
            self.assertEqual(progress["transport_sampling_seconds"], 1)
            self.assertEqual(progress["fps_topics"][0]["latency_avg_ms"], 0)
            self.assertIsNone(progress["fps_topics"][0]["latency_p95_ms"])
            (root / "transport_summary.csv").write_text(
                header + "60,/camera/depth_registered/points,1800,30,30,12,15,0,0\n"
            )
            final = build_performance_metrics(root)
            self.assertTrue(final["transport_final"])
            self.assertEqual(final["elapsed_seconds"], 60)
            self.assertEqual(final["fps_topics"][0]["topic"], "/camera/depth_registered/points")
            self.assertEqual(final["fps_topics"][0]["samples"], 1800)
            self.assertEqual(final["fps_topics"][0]["latency_p95_ms"], 15)

    def test_gpu_transport_metrics_include_stream_profiles(self):
        rows = [
            {"stream": "color", "phase": "cpu"},
            {"stream": "color", "phase": "cuda"},
            {"stream": "depth", "phase": "cuda"},
            {"stream": "colored_points", "phase": "cuda"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gpu_transport_progress.json").write_text(json.dumps({"rows": rows}))
            (root / "launch.log").write_text(
                "[camera.camera]: color Frame - Width: 1280 Height: 720 fps: 30 Format: MJPG\n"
                "[camera.camera]: depth Frame - Width: 848 Height: 480 fps: 30 Format: Y16\n"
            )
            metrics = build_performance_metrics(root)["gpu_transport"]["rows"]

        self.assertEqual(
            [(row["phase"], row["resolution"], row["stream_format"]) for row in metrics],
            [("cpu", "1280 x 720", "MJPG"), ("cuda", "1280 x 720", "MJPG"),
             ("cuda", "848 x 480", "Y16"), ("cuda", None, "XYZRGB")],
        )

    def test_dual_color_uses_driver_config_and_ignores_stream_options(self):
        payload = self.base_payload()
        payload["launch_config"] = "dual_color"
        payload["stream_options"]["color_width"] = "1280"
        args = _build_runner_args(payload, "performance", Path("/tmp/results"))
        config_index = args.index("--config-file-path")
        self.assertEqual(args[config_index + 1], "gemini305_dual_color.yaml")
        self.assertIn("device_preset=Dual Color Streams", args)
        self.assertNotIn("enable_color=true", args)
        self.assertNotIn("color_width=1280", args)

    def test_transport_overrides_only_the_selected_preset_streams(self):
        payload = self.base_payload()
        payload.update(mode="transport_latency", launch_config="dual_color", stream_options={
            "left_color_width": "1280", "right_color_format": "mjpg", "left_ir_width": "848",
        })
        self.assertEqual(validate_run_payload(payload), [])
        _, commands, _ = _build_shell_script(payload, Path("/tmp/results"))
        self.assertIn("--config-file-path gemini305_dual_color.yaml", commands[0])
        self.assertIn("--transport-preset-profile left_color_width=1280", commands[0])
        self.assertIn("--transport-preset-profile right_color_format=MJPG", commands[0])
        self.assertNotIn("left_ir_width", commands[0])

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

    def test_ros2_localhost_only_is_exported_and_unset_explicitly(self):
        enabled_command = _ros_localhost_environment_command("2", True)
        self.assertIn('case "${ROS_DISTRO:-}" in', enabled_command)
        self.assertIn("foxy|galactic|humble", enabled_command)
        self.assertIn("export ROS_LOCALHOST_ONLY=1", enabled_command)
        self.assertIn(
            "export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST", enabled_command
        )
        self.assertEqual(
            _ros_localhost_environment_command("2", False),
            "unset ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE",
        )
        self.assertEqual(_ros_localhost_environment_command("1", True), "")
        self.assertEqual(
            _ros_localhost_environment_command(
                "2", True, ros_distro_available=False
            ),
            "export ROS_LOCALHOST_ONLY=1\n"
            "export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST",
        )

        for distro, expected in (
            ("humble", "1|"),
            ("iron", "|LOCALHOST"),
            ("jazzy", "|LOCALHOST"),
            ("future", "|LOCALHOST"),
        ):
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    enabled_command
                    + '\nprintf "%s|%s" "${ROS_LOCALHOST_ONLY:-}" '
                    + '"${ROS_AUTOMATIC_DISCOVERY_RANGE:-}"',
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "ROS_DISTRO": distro},
            )
            self.assertEqual(completed.stdout, expected, distro)

        payload = self.base_payload()
        script, _commands, _shell = _build_shell_script(
            payload, Path("/tmp/localhost-results")
        )
        self.assertIn("export ROS_LOCALHOST_ONLY=1", script)
        self.assertIn("export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST", script)
        self.assertIn("[UI] ROS discovery is limited to localhost", script)

        payload["ros_localhost_only"] = False
        script, _commands, _shell = _build_shell_script(
            payload, Path("/tmp/localhost-results")
        )
        self.assertIn(
            "unset ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE", script
        )

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

    def test_ros_localhost_only_config_does_not_inherit_environment_and_persists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "ui_config.json"
            with patch.object(run_manager, "CONFIG_PATH", config_path), patch.dict(
                os.environ, {"ROS_LOCALHOST_ONLY": "1"}
            ):
                self.assertTrue(run_manager.load_config()["ros_localhost_only"])
                run_manager.save_config({"ros_localhost_only": True})
                self.assertTrue(run_manager.load_config()["ros_localhost_only"])
                run_manager.save_config({"ros_localhost_only": False})
                self.assertFalse(run_manager.load_config()["ros_localhost_only"])

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


def test_ros_setup_detection_uses_installed_versions_and_active_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(run_manager, "ROS_INSTALL_ROOT", tmp_path)
    for name in ("ROS_DISTRO", "AMENT_PREFIX_PATH", "CMAKE_PREFIX_PATH"):
        monkeypatch.delenv(name, raising=False)

    def install(distro, version):
        prefix = tmp_path / distro
        directory = "environment" if version == "2" else "catkin_env_hook"
        hook = prefix / "share" / "ros_environment" / directory / "1.ros_version.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text(f"export ROS_VERSION={version}\n")
        setup = prefix / "setup.bash"
        setup.write_text("# setup\n")
        return str(setup)

    assert run_manager._default_ros_setup_for_version("2") == ""
    jazzy = install("jazzy", "2")
    one = install("one", "1")
    assert run_manager.setup_defaults()["2"]["ros_setup"] == jazzy
    assert run_manager.setup_defaults()["1"]["ros_setup"] == one
    humble = install("humble", "2")
    assert run_manager._default_ros_setup_for_version("2") == ""
    monkeypatch.setenv("ROS_DISTRO", "humble")
    assert run_manager._default_ros_setup_for_version("2") == humble
    assert run_manager._default_ros_setup_for_version("1") == one
    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    assert run_manager._default_ros_setup_for_version("2") == jazzy
    (tmp_path / "jazzy" / "setup.bash").unlink()
    assert run_manager._default_ros_setup_for_version("2") == humble


def test_saved_custom_ros_setup_is_preserved_and_missing_old_default_is_migrated(tmp_path, monkeypatch):
    old = str(tmp_path / "old" / "setup.bash")
    custom = str(tmp_path / "custom" / "setup.bash")
    monkeypatch.setattr(run_manager, "DEFAULT_ROS_SETUP", old)
    monkeypatch.setattr(run_manager, "_default_ros_setup_for_version", lambda _: "/detected/setup.bash")
    assert run_manager._saved_or_detected_ros_setup(old, "2") == "/detected/setup.bash"
    assert run_manager._saved_or_detected_ros_setup(custom, "2") == custom
    assert run_manager._saved_or_detected_ros_setup("", "2") == "/detected/setup.bash"


def test_ros_setup_prefers_active_custom_prefix_and_preserves_overlay_order(tmp_path, monkeypatch):
    installs = tmp_path / "opt" / "ros"
    monkeypatch.setattr(run_manager, "ROS_INSTALL_ROOT", installs)
    monkeypatch.setenv("ROS_VERSION", "2")
    monkeypatch.setenv("ROS_DISTRO", "jazzy")

    def install(prefix, version):
        directory = "environment" if version == "2" else "catkin_env_hook"
        hook = prefix / "share" / "ros_environment" / directory / "1.ros_version.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text(f"export ROS_VERSION={version}\n")
        setup = prefix / "setup.bash"
        setup.write_text("# setup\n")
        return str(setup)

    system = install(installs / "jazzy", "2")
    custom = install(tmp_path / "custom" / "install", "2")
    ros1 = install(installs / "one", "1")
    overlay = tmp_path / "camera_overlay"
    overlay.mkdir()
    (overlay / "setup.bash").write_text("# No ros_environment package\n")
    monkeypatch.setenv("AMENT_PREFIX_PATH", os.pathsep.join((str(overlay), str(Path(custom).parent), str(Path(system).parent))))
    monkeypatch.setenv("CMAKE_PREFIX_PATH", str(Path(ros1).parent))
    assert run_manager._default_ros_setup_for_version("2") == custom
    assert run_manager._default_ros_setup_for_version("1") == ros1
    monkeypatch.setenv("AMENT_PREFIX_PATH", os.pathsep.join((str(Path(system).parent), str(Path(custom).parent))))
    assert run_manager._default_ros_setup_for_version("2") == system

    # An alias of the same install must not create multiple candidates.
    alias = tmp_path / "alias"
    alias.symlink_to(Path(custom).parent, target_is_directory=True)
    monkeypatch.setenv("AMENT_PREFIX_PATH", os.pathsep.join((str(alias), str(Path(custom).parent))))
    assert run_manager._default_ros_setup_for_version("2") == custom
