from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .loader import LaunchScenarioSpec, ServiceSpec, TopicSpec


def expand_camera_template(value: Optional[str], camera_name: str) -> Optional[str]:
    if value is None:
        return None
    return (
        value.replace("{camera}", camera_name)
        .replace("{camera_name}", camera_name)
        .replace("${camera}", camera_name)
        .replace("${camera_name}", camera_name)
    )


def expand_topic_spec(topic: TopicSpec, camera_name: str) -> TopicSpec:
    return replace(
        topic,
        name=expand_camera_template(topic.name, camera_name) or topic.name,
        paired_topic=expand_camera_template(topic.paired_topic, camera_name),
    )


def expand_topic_specs(topics: list[TopicSpec], camera_name: str) -> list[TopicSpec]:
    return [expand_topic_spec(topic, camera_name) for topic in topics]


def expand_service_spec(service: ServiceSpec, camera_name: str) -> ServiceSpec:
    return replace(
        service,
        name=expand_camera_template(service.name, camera_name) or service.name,
        getter_name=expand_camera_template(service.getter_name, camera_name),
        keepalive_topics=expand_topic_specs(service.keepalive_topics, camera_name),
    )


def expand_service_specs(services: list[ServiceSpec], camera_name: str) -> list[ServiceSpec]:
    return [expand_service_spec(service, camera_name) for service in services]


def expand_launch_scenario(
    scenario: LaunchScenarioSpec,
    camera_name: str,
) -> LaunchScenarioSpec:
    return replace(
        scenario,
        topics=expand_topic_specs(scenario.topics, camera_name),
        services=expand_service_specs(scenario.services, camera_name),
    )
