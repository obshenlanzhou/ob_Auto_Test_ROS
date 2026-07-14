from pathlib import Path
from types import SimpleNamespace

import pytest

from orbbec_camera_auto_test.checks import topics
from orbbec_camera_auto_test.profile.loader import TopicSpec


class FakeSubscription:
    def __init__(self, topic_name: str) -> None:
        self.topic_name = topic_name


class FakeNode:
    def __init__(self, messages=None) -> None:
        self.active_topics = set()
        self.messages = messages or {}
        self.subscription_order = []

    def create_subscription(self, msg_type, topic_name, callback, qos_profile):
        del msg_type, qos_profile
        self.active_topics.add(topic_name)
        self.subscription_order.append(topic_name)
        for message in self.messages.get(topic_name, []):
            callback(message)
        return FakeSubscription(topic_name)

    def destroy_subscription(self, subscription) -> None:
        self.active_topics.remove(subscription.topic_name)


class FakeHarness:
    ros_version = "1"

    def __init__(self, node: FakeNode) -> None:
        self.node = node
        self.spin_until_calls = 0

    def topic_is_supported(self, topic_name, type_name):
        del topic_name, type_name
        return True, "topic advertised"

    def spin_until(self, predicate, timeout, description):
        del timeout, description
        self.spin_until_calls += 1
        if not predicate():
            raise TimeoutError("condition not met")


def test_metadata_keeps_paired_image_subscription_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = FakeNode(
        {
            "/camera/color/metadata": [SimpleNamespace(json_data='{"frame": 1}')],
            "/camera/color/image_raw": [SimpleNamespace(width=1280, height=720)],
        }
    )
    harness = FakeHarness(node)
    harness.wait_for_message = lambda *args, **kwargs: pytest.fail(
        "paired topics must use simultaneous subscriptions"
    )
    harness.wait_for_topic = lambda *args, **kwargs: (
        pytest.fail("paired image subscription was destroyed too early")
        if "/camera/color/image_raw" not in node.active_topics
        else None
    )
    monkeypatch.setattr(topics, "resolve_message_type", lambda name, version: name)
    metadata_spec = TopicSpec(
        name="/camera/color/metadata",
        type="orbbec_camera_msgs/msg/Metadata",
        validator="metadata",
        paired_topic="/camera/color/image_raw",
        timeout=10.0,
    )
    advertised_spec = TopicSpec(
        name="/camera/color/image_raw/theora",
        type="theora_image_transport/msg/Packet",
        mode="advertised",
        timeout=10.0,
    )

    result = topics.run_topic_checks(
        harness, [metadata_spec, advertised_spec], tmp_path / "topic.log"
    )

    assert result[0]["status"] == "passed"
    assert result[1]["status"] == "passed"
    assert node.subscription_order == [
        "/camera/color/image_raw",
        "/camera/color/metadata",
    ]
    assert harness.spin_until_calls == 2
    assert node.active_topics == set()


def test_tf_static_accepts_later_batch_with_optical_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    without_optical = SimpleNamespace(
        transforms=[SimpleNamespace(child_frame_id="camera_link")]
    )
    with_optical = SimpleNamespace(
        transforms=[SimpleNamespace(child_frame_id="camera_color_optical_frame")]
    )
    node = FakeNode({"/tf_static": [without_optical, with_optical]})
    harness = FakeHarness(node)
    harness.wait_for_message = lambda *args, **kwargs: without_optical
    monkeypatch.setattr(topics, "resolve_message_type", lambda name, version: name)
    spec = TopicSpec(
        name="/tf_static",
        type="tf2_msgs/msg/TFMessage",
        validator="tf_static",
        timeout=10.0,
    )

    result = topics.run_topic_checks(harness, [spec], tmp_path / "topic.log")

    assert result[0]["status"] == "passed"
    assert result[0]["metrics"] == {"transform_count": 1}
