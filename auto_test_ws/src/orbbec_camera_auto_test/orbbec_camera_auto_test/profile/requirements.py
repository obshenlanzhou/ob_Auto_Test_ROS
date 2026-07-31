from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .templating import expand_camera_template


PACKAGE_NAME = "orbbec_camera_auto_test"
DEFAULT_REQUIREMENTS_FILE = "functional_required_interfaces.yaml"


@dataclass(frozen=True)
class RequiredInterfaceRule:
    name: str
    required_topics: List[str] = field(default_factory=list)
    required_services: List[str] = field(default_factory=list)
    when: Dict[str, Any] = field(default_factory=dict)
    ros_versions: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LaunchRequirementProfile:
    name: str
    ros_version: str
    launch_file: str
    camera_models: List[str]
    defaults: Dict[str, Any]
    config_overrides: Dict[str, Dict[str, Any]]
    rules: List[RequiredInterfaceRule]


@dataclass(frozen=True)
class ResolvedInterfaceRequirements:
    profile_name: str
    camera_models: List[str]
    matched_rules: List[str]
    required_topics: List[str]
    required_services: List[str]
    effective_launch_args: Dict[str, Any]


def _requirements_candidates(package_root: Optional[Path] = None) -> List[Path]:
    base_dirs = (
        [package_root]
        if package_root is not None
        else [Path(__file__).resolve().parents[2]]
    )
    try:
        from ament_index_python.packages import get_package_share_directory

        base_dirs.append(Path(get_package_share_directory(PACKAGE_NAME)))
    except Exception:  # noqa: BLE001
        pass
    return [
        base_dir / "profiles" / "base" / DEFAULT_REQUIREMENTS_FILE
        for base_dir in base_dirs
    ]


def resolve_requirements_path(package_root: Optional[Path] = None) -> Path:
    for candidate in _requirements_candidates(package_root):
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Required-interface table not found: {DEFAULT_REQUIREMENTS_FILE}"
    )


def _load_requirements_data(
    requirements_path: Optional[Path] = None,
    package_root: Optional[Path] = None,
) -> Dict[str, Any]:
    path = (
        Path(requirements_path).resolve()
        if requirements_path is not None
        else resolve_requirements_path(package_root)
    )
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Required-interface table {path} must contain a mapping")
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError(f"Required-interface table {path} has unsupported schema_version")
    return data


def _rule_from_dict(data: Dict[str, Any]) -> RequiredInterfaceRule:
    return RequiredInterfaceRule(
        name=str(data["name"]),
        required_topics=[str(item) for item in data.get("required_topics", [])],
        required_services=[str(item) for item in data.get("required_services", [])],
        when=dict(data.get("when", {})),
        ros_versions=[str(item) for item in data.get("ros_versions", [])],
    )


def load_launch_requirement_profile(
    launch_file: str,
    ros_version: str,
    requirements_path: Optional[Path] = None,
    package_root: Optional[Path] = None,
) -> LaunchRequirementProfile:
    data = _load_requirements_data(
        requirements_path=requirements_path,
        package_root=package_root,
    )
    launch_name = Path(launch_file).name
    version = str(ros_version)
    matches = []
    for raw_profile in data.get("launch_profiles", []):
        versions = [str(item) for item in raw_profile.get("ros_versions", [])]
        launch_files = [str(item) for item in raw_profile.get("launch_files", [])]
        if version in versions and launch_name in launch_files:
            matches.append(raw_profile)

    if not matches:
        raise ValueError(
            f"no required-interface profile for ROS {version} launch '{launch_name}'"
        )
    if len(matches) > 1:
        names = ", ".join(str(item.get("name", "")) for item in matches)
        raise ValueError(
            f"multiple required-interface profiles match ROS {version} "
            f"launch '{launch_name}': {names}"
        )

    raw_profile = matches[0]
    rules = [_rule_from_dict(item) for item in data.get("rules", [])]
    return LaunchRequirementProfile(
        name=str(raw_profile["name"]),
        ros_version=version,
        launch_file=launch_name,
        camera_models=[
            str(item) for item in raw_profile.get("camera_models", [])
        ],
        defaults=dict(raw_profile.get("defaults", {})),
        config_overrides={
            str(name): dict(values)
            for name, values in raw_profile.get("config_overrides", {}).items()
        },
        rules=rules,
    )


def _normalized_scalar(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text
    return value


def _rule_matches(
    rule: RequiredInterfaceRule,
    ros_version: str,
    effective_launch_args: Dict[str, Any],
) -> bool:
    if rule.ros_versions and str(ros_version) not in rule.ros_versions:
        return False
    for key, expected in rule.when.items():
        if key not in effective_launch_args:
            return False
        actual = _normalized_scalar(effective_launch_args[key])
        if actual != _normalized_scalar(expected):
            return False
    return True


def _unique_in_order(items: List[str]) -> List[str]:
    return list(dict.fromkeys(items))


def _load_config_overrides(
    profile: LaunchRequirementProfile,
    config_file_path: Any,
) -> Dict[str, Any]:
    config_text = str(config_file_path or "").strip()
    if not config_text:
        return {}

    config_path = Path(config_text).expanduser()
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"functional config file must contain a mapping: {config_path}"
            )
        return data

    config_name = config_path.name
    if config_name in profile.config_overrides:
        return dict(profile.config_overrides[config_name])
    raise ValueError(
        "cannot resolve functional config file for required-interface "
        f"evaluation: {config_text}; use an existing path or add a "
        f"config_overrides entry to profile '{profile.name}'"
    )


def resolve_required_interfaces(
    profile: LaunchRequirementProfile,
    launch_args: Dict[str, Any],
    camera_name: str,
) -> ResolvedInterfaceRequirements:
    effective_launch_args = dict(profile.defaults)
    effective_launch_args.update(launch_args)
    effective_launch_args.update(
        _load_config_overrides(
            profile, effective_launch_args.get("config_file_path")
        )
    )
    matched_rules = [
        rule
        for rule in profile.rules
        if _rule_matches(rule, profile.ros_version, effective_launch_args)
    ]
    topics = [
        expand_camera_template(name, camera_name) or name
        for rule in matched_rules
        for name in rule.required_topics
    ]
    services = [
        expand_camera_template(name, camera_name) or name
        for rule in matched_rules
        for name in rule.required_services
    ]
    return ResolvedInterfaceRequirements(
        profile_name=profile.name,
        camera_models=profile.camera_models,
        matched_rules=[rule.name for rule in matched_rules],
        required_topics=_unique_in_order(topics),
        required_services=_unique_in_order(services),
        effective_launch_args=effective_launch_args,
    )
