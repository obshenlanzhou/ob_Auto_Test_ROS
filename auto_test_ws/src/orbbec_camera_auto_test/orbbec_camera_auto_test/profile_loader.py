from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class TopicSpec:
    name: str
    type: str = ""
    mode: str = "message"
    validator: str = "any"
    paired_topic: Optional[str] = None
    timeout: Optional[float] = None
    qos: str = "default"
    ideal_fps_key: Optional[str] = None
    ideal_fps: float = 0.0


@dataclass
class ServiceSpec:
    name: str
    type: str
    mode: str
    request: Dict[str, Any] = field(default_factory=dict)
    alternate_request: Dict[str, Any] = field(default_factory=dict)
    response_checks: List[str] = field(default_factory=list)
    getter_name: Optional[str] = None
    getter_type: Optional[str] = None
    getter_field: str = "data"
    request_field: str = "data"
    wait_after_call: float = 0.0
    target_subdir: Optional[str] = None
    min_new_files: int = 1
    keepalive_topics: List[TopicSpec] = field(default_factory=list)


@dataclass
class LaunchScenarioSpec:
    name: str
    launch_args: Dict[str, Any] = field(default_factory=dict)
    topic_defaults: Dict[str, Any] = field(default_factory=dict)
    topics: List[TopicSpec] = field(default_factory=list)
    services: List[ServiceSpec] = field(default_factory=list)


@dataclass
class ExternalLoadSpec:
    type: str = "none"
    workers: int = 0
    start_after_seconds: float = 0.0
    stop_after_seconds: float = 0.0
    args: List[str] = field(default_factory=list)


@dataclass
class PerformanceScenarioSpec:
    name: str
    description: str = ""
    duration: Optional[float] = None
    launch_args: Dict[str, Any] = field(default_factory=dict)
    topics: List[TopicSpec] = field(default_factory=list)
    load: Optional[ExternalLoadSpec] = None


@dataclass
class CameraProfile:
    profile_name: str
    launch_file: str
    default_launch_args: Dict[str, Any]
    launch_scenarios: List[LaunchScenarioSpec]
    performance_topics: List[TopicSpec]
    performance_scenarios: List[PerformanceScenarioSpec]


def _default_timeout(defaults: Dict[str, Any], fallback: float = 30.0) -> float:
    return float(defaults.get("timeout", fallback))


def _topic_from_dict(data: Dict[str, Any]) -> TopicSpec:
    return TopicSpec(
        name=data["name"],
        type=data.get("type", ""),
        mode=data.get("mode", "message"),
        validator=data.get("validator", "any"),
        paired_topic=data.get("paired_topic"),
        timeout=(float(data["timeout"]) if "timeout" in data else None),
        qos=data.get("qos", "default"),
        ideal_fps_key=data.get("ideal_fps_key"),
        ideal_fps=float(data.get("ideal_fps", 0.0) or 0.0),
    )


def _topic_with_defaults(topic: TopicSpec, default_timeout: float) -> TopicSpec:
    if topic.timeout is None:
        topic.timeout = default_timeout
    return topic


def _topics_from_dicts(items: List[Dict[str, Any]], default_timeout: float) -> List[TopicSpec]:
    return [_topic_with_defaults(_topic_from_dict(item), default_timeout) for item in items]


def _service_from_dict(data: Dict[str, Any], topic_default_timeout: float) -> ServiceSpec:
    return ServiceSpec(
        name=data["name"],
        type=data["type"],
        mode=data["mode"],
        request=dict(data.get("request", {})),
        alternate_request=dict(data.get("alternate_request", {})),
        response_checks=list(data.get("response_checks", [])),
        getter_name=data.get("getter_name"),
        getter_type=data.get("getter_type"),
        getter_field=data.get("getter_field", "data"),
        request_field=data.get("request_field", "data"),
        wait_after_call=float(data.get("wait_after_call", 0.0)),
        target_subdir=data.get("target_subdir"),
        min_new_files=int(data.get("min_new_files", 1)),
        keepalive_topics=_topics_from_dicts(data.get("keepalive_topics", []), topic_default_timeout),
    )


def _launch_scenario_from_dict(data: Dict[str, Any]) -> LaunchScenarioSpec:
    topic_defaults = dict(data.get("topic_defaults", {}))
    default_timeout = _default_timeout(topic_defaults)
    return LaunchScenarioSpec(
        name=data["name"],
        launch_args=dict(data.get("launch_args", {})),
        topic_defaults=topic_defaults,
        topics=_topics_from_dicts(data.get("topics", []), default_timeout),
        services=[
            _service_from_dict(item, topic_default_timeout=default_timeout)
            for item in data.get("services", [])
        ],
    )


def _external_load_from_dict(data: Dict[str, Any]) -> ExternalLoadSpec:
    raw_args = data.get("args", [])
    if raw_args is None:
        raw_args = []
    if isinstance(raw_args, str):
        args = [raw_args]
    else:
        args = [str(item) for item in raw_args]
    return ExternalLoadSpec(
        type=str(data.get("type", "none")),
        workers=int(data.get("workers", 0) or 0),
        start_after_seconds=float(data.get("start_after_seconds", 0.0) or 0.0),
        stop_after_seconds=float(data.get("stop_after_seconds", 0.0) or 0.0),
        args=args,
    )


def _performance_scenario_from_dict(
    data: Dict[str, Any],
    default_topics: List[Dict[str, Any]],
    default_timeout: float,
) -> PerformanceScenarioSpec:
    raw_topics = data.get("topics", default_topics)
    raw_load = data.get("load")
    return PerformanceScenarioSpec(
        name=data["name"],
        description=str(data.get("description", "")),
        duration=(float(data["duration"]) if "duration" in data else None),
        launch_args=dict(data.get("launch_args", {})),
        topics=_topics_from_dicts(raw_topics, default_timeout),
        load=_external_load_from_dict(raw_load) if raw_load else None,
    )


def resolve_profile_path(profile: str, package_root: Optional[Path] = None) -> Path:
    candidate = Path(profile)
    if candidate.is_file():
        return candidate.resolve()

    base_dir = package_root or Path(__file__).resolve().parents[1]
    resolved = base_dir / "profiles" / f"{profile}.yaml"
    if not resolved.is_file():
        raise FileNotFoundError(f"Profile not found: {profile}")
    return resolved.resolve()


def load_camera_profile(profile: str, package_root: Optional[Path] = None) -> CameraProfile:
    profile_path = resolve_profile_path(profile, package_root=package_root)
    with profile_path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}

    required_keys = {
        "profile_name",
        "launch_file",
        "default_launch_args",
        "launch_scenarios",
    }
    missing = sorted(required_keys.difference(data))
    if missing:
        raise ValueError(f"Profile {profile_path} is missing keys: {', '.join(missing)}")

    performance_topic_defaults = dict(data.get("performance_topic_defaults", {}))
    default_performance_timeout = _default_timeout(performance_topic_defaults)
    legacy_performance_topics = list(data.get("performance_topics", []))
    raw_performance_scenarios = list(data.get("performance_scenarios", []))

    if not raw_performance_scenarios and not legacy_performance_topics:
        raise ValueError(
            f"Profile {profile_path} must define either performance_topics or performance_scenarios"
        )

    if raw_performance_scenarios:
        performance_scenarios = [
            _performance_scenario_from_dict(
                item,
                default_topics=legacy_performance_topics,
                default_timeout=default_performance_timeout,
            )
            for item in raw_performance_scenarios
        ]
    else:
        performance_scenarios = [
            PerformanceScenarioSpec(
                name="default",
                description="Legacy performance scenario",
                launch_args={},
                topics=_topics_from_dicts(legacy_performance_topics, default_performance_timeout),
                load=None,
            )
        ]

    return CameraProfile(
        profile_name=data["profile_name"],
        launch_file=data["launch_file"],
        default_launch_args=dict(data.get("default_launch_args", {})),
        launch_scenarios=[
            _launch_scenario_from_dict(item) for item in data.get("launch_scenarios", [])
        ],
        performance_topics=_topics_from_dicts(
            legacy_performance_topics,
            default_performance_timeout,
        ),
        performance_scenarios=performance_scenarios,
    )
