import unittest

from orbbec_camera_auto_test.runners.performance import build_generic_performance_profile


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


if __name__ == "__main__":
    unittest.main()
