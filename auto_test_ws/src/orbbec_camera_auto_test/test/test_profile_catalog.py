from pathlib import Path
import unittest

from orbbec_camera_auto_test.profile.loader import load_camera_profile
from orbbec_camera_auto_test.profile.merger import load_merged_profile_data


BASE_PROFILES = Path(__file__).resolve().parents[1] / "profiles" / "base"


class UnifiedInterfaceCatalogTest(unittest.TestCase):
    def test_generic_functional_catalog_is_loadable(self):
        profile = load_camera_profile("all_topics_services", profile_type="functional")
        self.assertEqual(profile.profile_name, "generic_functional")
        self.assertEqual(profile.launch_file, "")
        self.assertEqual([item.name for item in profile.launch_scenarios], ["default"])

    def test_catalog_has_unique_topic_and_service_names(self):
        catalog = load_merged_profile_data(BASE_PROFILES / "all_topics_services.yaml")
        scenario = catalog["launch_scenarios"][0]

        for key in ("topics", "services"):
            names = [item["name"] for item in scenario[key]]
            with self.subTest(key=key):
                self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
