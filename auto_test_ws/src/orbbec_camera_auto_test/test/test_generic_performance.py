import json
import unittest
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from orbbec_camera_auto_test.core.environment_info import camera_environment_from_launch_log
from orbbec_camera_auto_test.core.reporter import build_performance_summary
from orbbec_camera_auto_test.runners import transport
from orbbec_camera_auto_test.runners.performance import build_generic_performance_profile
from orbbec_camera_auto_test.runners.transport import _read_summary


class GenericPerformanceProfileTest(unittest.TestCase):
    def test_fixed_scenarios_and_discovered_stream_catalog(self):
        profile = build_generic_performance_profile()
        self.assertEqual(
            [scenario.name for scenario in profile.performance_scenarios],
            ["default", "stress", "drop_frame"],
        )
        topic_names = {topic.name for topic in profile.performance_scenarios[0].topics}
        self.assertIn("/{camera}/left_color/image_raw", topic_names)
        self.assertIn("/{camera}/gyro_accel/sample", topic_names)
        self.assertNotIn("/{camera}/color/camera_info", topic_names)

    def test_drop_frame_enables_driver_and_receiver_timestamps(self):
        scenario = build_generic_performance_profile().performance_scenarios[-1]
        self.assertEqual(scenario.launch_args["enable_frame_drop_log"], True)
        self.assertEqual(
            scenario.launch_args["frame_timestamp_csv_file"],
            "{results_dir}/driver_frame_timestamp.csv",
        )
        self.assertTrue(scenario.frame_timestamps.enabled)
        self.assertNotIn("enable_frame_timestamp_csv", scenario.launch_args)


class TransportSummaryTest(unittest.TestCase):
    def test_transport_camera_environment_uses_the_actual_driver_banner(self):
        with TemporaryDirectory() as directory:
            log = Path(directory) / "launch.log"
            log.write_text(
                "[component_container-1] [INFO] [123.00] [other.other]: Device Other connected\n"
                "[component_container-1] [INFO] [123.01] [camera.camera]: Device Orbbec Gemini 336 connected\n"
                "[component_container-1] [INFO] [123.02] [camera.camera]: Firmware version: 1.8.10\n"
                "[component_container-1] [INFO] [123.03] [camera.camera]: ROS Wrapper version: 2.11.1\n"
                "[component_container-1] [INFO] [123.04] [other.other]: SDK version: 1.0.0\n"
                "[component_container-1] [INFO] [123.05] [camera.camera]: SDK version: 2.10.6\n",
                encoding="utf-8",
            )
            info = camera_environment_from_launch_log(log, "/camera")
            self.assertEqual(info, {
                "camera_model": "Orbbec Gemini 336",
                "firmware_version": "1.8.10",
                "ros_sdk_version": "2.11.1",
                "ob_sdk_version": "2.10.6",
            })
            report = build_performance_summary({
                "profile_name": "generic_performance",
                "scenario_name": "transport_latency",
                "transport_mode": "cross_process",
                "environment": {"camera": info},
            })
            self.assertIn("- Camera model: `Orbbec Gemini 336`", report)
            self.assertIn("- ob_sdk_version: `2.10.6`", report)

    def test_reliable_results_require_the_subscriber_to_report_reliable(self):
        header = "topic,samples,avg_fps,latency_avg_ms,latency_p95_ms,invalid_stamps,delivery_mismatches"
        sample = "/camera/color/image_raw,30,30,25,26,0,0"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "transport_summary.csv"
            path.write_text(f"{header}\n{sample}\n", encoding="utf-8")
            self.assertEqual(_read_summary(path, "best_effort")["/camera/color/image_raw"]["message_count"], 30)
            with self.assertRaisesRegex(RuntimeError, "did not report the requested QoS reliable"):
                _read_summary(path, "reliable")
            path.write_text(f"{header},transport_reliability\n{sample},reliable\n", encoding="utf-8")
            self.assertEqual(_read_summary(path, "reliable")["/camera/color/image_raw"]["message_count"], 30)


def test_transport_excludes_resource_samples_during_camera_shutdown(tmp_path, monkeypatch):
    launch_file = tmp_path / "camera.launch.py"
    launch_file.touch()
    (tmp_path / "ros_distro.txt").write_text("jazzy")
    progress = tmp_path / "transport_progress.csv"
    summary = tmp_path / "transport_summary.csv"
    session = Mock()
    session.start.side_effect = progress.touch
    session.process.poll.side_effect = [None, None, None, 0]
    session.process.returncode = 0
    session.sample_camera_process_tree.side_effect = [
        {"cpu_percent": 0.0, "memory_rss_mb": 170.0, "pid_count": 1},
        {"cpu_percent": 1.23, "memory_rss_mb": 170.0, "pid_count": 1},
        {"cpu_percent": 0.70, "memory_rss_mb": 168.0, "pid_count": 1},
    ]

    def tick(_seconds):
        if session.sample_camera_process_tree.call_count == 2:
            summary.write_text(
                "topic,samples,avg_fps,latency_avg_ms,latency_p95_ms,invalid_stamps,delivery_mismatches\n"
                "/camera/color/image_raw,60,30,25,26,0,0\n"
            )

    clock = SimpleNamespace(monotonic=count().__next__, sleep=tick)
    monkeypatch.setattr(transport, "time", clock)
    monkeypatch.setattr(transport, "TestSession", lambda *args, **kwargs: session)
    monkeypatch.setattr(transport, "capture_runtime_env", lambda *args, **kwargs: {"ROS_DISTRO": "jazzy"})
    monkeypatch.setattr(transport, "_configured_setup", lambda _: None)
    monkeypatch.setattr(transport, "_package_share", lambda *_: tmp_path)
    monkeypatch.setattr(transport, "_loaded_sdk_paths", lambda _: set())
    args = SimpleNamespace(
        ros_version="2", ros_setup="", driver_setup="", launch_file=str(launch_file),
        transport_mode="cross_process", transport_reliability="best_effort",
        transport_warmup_frames=30, transport_warmup_timeout_sec=10, transport_preset_profile=[],
    )

    assert transport.run_transport_test(args, {}, tmp_path, {}, 2, Mock(), lambda _: {}) == 0
    assert session.sample_camera_process_tree.call_count == 2
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["system_summary"]["avg_cpu_percent"] == 1.23
    assert (tmp_path / "system_usage.csv").read_text().splitlines()[1:] == ["1.000,1,1.230,170.000"]


if __name__ == "__main__":
    unittest.main()
