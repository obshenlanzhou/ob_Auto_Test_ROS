from pathlib import Path
import unittest

from orbbec_camera_auto_test.profile.merger import load_merged_profile_data


BASE_PROFILES = Path(__file__).resolve().parents[1] / "profiles" / "base"


class UnifiedInterfaceCatalogTest(unittest.TestCase):
    def test_functional_bases_share_one_interface_catalog(self):
        catalog = load_merged_profile_data(BASE_PROFILES / "all_topics_services.yaml")
        expected_scenarios = catalog["launch_scenarios"]

        for filename in (
            "depth_color_ir_functional.yaml",
            "depth_color_left_right_ir_functional.yaml",
            "double_color_functional.yaml",
        ):
            with self.subTest(filename=filename):
                merged = load_merged_profile_data(BASE_PROFILES / filename)
                self.assertEqual(merged["launch_scenarios"], expected_scenarios)

    def test_catalog_has_unique_topic_and_service_names(self):
        catalog = load_merged_profile_data(BASE_PROFILES / "all_topics_services.yaml")
        scenario = catalog["launch_scenarios"][0]

        for key in ("topics", "services"):
            names = [item["name"] for item in scenario[key]]
            with self.subTest(key=key):
                self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
