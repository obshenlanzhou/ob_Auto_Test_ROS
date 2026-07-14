from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from orbbec_camera_auto_test.checks.services import (
    run_service_checks,
    select_discovered_service_specs,
)
from orbbec_camera_auto_test.checks.topics import (
    run_topic_checks,
    select_discovered_topic_specs,
)
from orbbec_camera_auto_test.profile.loader import ServiceSpec, TopicSpec


class FakeHarness:
    ros_version = "2"

    def __init__(self, *, topic_result=(True, "topic advertised"), service_result=None):
        self.topic_result = topic_result
        self.service_result = service_result or (True, "service advertised")
        self.waited_for_topic = False

    def topic_is_supported(self, name, type_name=None):
        del name, type_name
        return self.topic_result

    def service_is_supported(self, name, type_name=None):
        del name, type_name
        return self.service_result

    def wait_for_topic(self, name, topic_type=None, timeout=30.0):
        del name, topic_type, timeout
        self.waited_for_topic = True


class DiscoveryFilteringTest(unittest.TestCase):
    def test_catalog_is_filtered_to_discovered_interfaces(self):
        topics = [
            TopicSpec(name="/found"),
            TopicSpec(name="/missing"),
            TopicSpec(name="/paired", paired_topic="/missing_pair"),
        ]
        services = [
            ServiceSpec(name="/read", type="T", mode="read"),
            ServiceSpec(
                name="/roundtrip",
                type="T",
                mode="roundtrip_int",
                getter_name="/getter",
            ),
        ]

        self.assertEqual(
            [item.name for item in select_discovered_topic_specs(topics, {"/found"})],
            ["/found"],
        )
        self.assertEqual(
            [
                item.name
                for item in select_discovered_service_specs(
                    services, {"/read", "/roundtrip"}
                )
            ],
            ["/read"],
        )

    def test_missing_topic_is_skipped_without_waiting(self):
        harness = FakeHarness(
            topic_result=(False, "topic not advertised: /camera/missing")
        )
        spec = TopicSpec(name="/camera/missing", mode="advertised", timeout=10.0)
        with TemporaryDirectory() as directory:
            results = run_topic_checks(harness, [spec], Path(directory) / "topic.log")

        self.assertEqual(results[0]["status"], "skipped")
        self.assertFalse(harness.waited_for_topic)

    def test_discovered_topic_type_mismatch_is_failed(self):
        harness = FakeHarness(
            topic_result=(
                False,
                "topic type mismatch for /camera/topic: expected A, got B",
            )
        )
        spec = TopicSpec(name="/camera/topic", type="A", mode="advertised")
        with TemporaryDirectory() as directory:
            results = run_topic_checks(harness, [spec], Path(directory) / "topic.log")

        self.assertEqual(results[0]["status"], "failed")
        self.assertFalse(harness.waited_for_topic)

    def test_missing_service_is_skipped(self):
        harness = FakeHarness(
            service_result=(False, "service not advertised: /camera/missing")
        )
        spec = ServiceSpec(name="/camera/missing", type="pkg/srv/Type", mode="advertised")
        with TemporaryDirectory() as directory:
            results = run_service_checks(harness, [spec], Path(directory) / "service.log")

        self.assertEqual(results[0]["status"], "skipped")

    def test_discovered_service_type_mismatch_is_failed(self):
        harness = FakeHarness(
            service_result=(
                False,
                "service type mismatch for /camera/service: expected A, got B",
            )
        )
        spec = ServiceSpec(name="/camera/service", type="A", mode="advertised")
        with TemporaryDirectory() as directory:
            results = run_service_checks(harness, [spec], Path(directory) / "service.log")

        self.assertEqual(results[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
