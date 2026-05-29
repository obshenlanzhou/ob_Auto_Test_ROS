from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml


def _as_list(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _merge_named_lists(base: List[Any], override: List[Any]) -> List[Any]:
    if not all(isinstance(item, dict) and "name" in item for item in base + override):
        return deepcopy(override)

    merged_by_name = {item["name"]: deepcopy(item) for item in base}
    order = [item["name"] for item in base]
    for item in override:
        name = item["name"]
        if name in merged_by_name:
            merged_by_name[name] = deep_merge(merged_by_name[name], item)
        else:
            order.append(name)
            merged_by_name[name] = deepcopy(item)
    return [merged_by_name[name] for name in order if merged_by_name[name].get("enabled", True)]


def _apply_remove_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    remove_rules = payload.pop("remove", {}) or {}
    if not isinstance(remove_rules, dict):
        return payload

    for key, names in remove_rules.items():
        if key not in payload or not isinstance(payload[key], list):
            continue
        remove_names = set(str(name) for name in _as_list(names))
        payload[key] = [
            item
            for item in payload[key]
            if not (isinstance(item, dict) and str(item.get("name", "")) in remove_names)
        ]
    return payload


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if key in {"extends", "base"}:
            continue
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        elif isinstance(result.get(key), list) and isinstance(value, list):
            result[key] = _merge_named_lists(result[key], value)
        else:
            result[key] = deepcopy(value)
    return _apply_remove_rules(result)


def _profiles_root_for(profile_path: Path) -> Path:
    for parent in [profile_path.parent, *profile_path.parents]:
        if parent.name == "profiles":
            return parent
    return profile_path.parent.parent


def _resolve_parent_path(profile_path: Path, parent: str) -> Path:
    candidate = Path(parent)
    if candidate.is_file():
        return candidate.resolve()

    relative_candidate = (profile_path.parent / candidate).resolve()
    if relative_candidate.is_file():
        return relative_candidate

    profiles_dir = _profiles_root_for(profile_path)
    profiles_candidate = (profiles_dir / candidate).resolve()
    if profiles_candidate.is_file():
        return profiles_candidate

    if candidate.suffix != ".yaml":
        yaml_candidate = (profiles_dir / f"{parent}.yaml").resolve()
        if yaml_candidate.is_file():
            return yaml_candidate

    raise FileNotFoundError(f"Base profile not found for {profile_path}: {parent}")


def _parent_entries(data: Dict[str, Any]) -> Iterable[str]:
    return [str(item) for item in _as_list(data.get("extends") or data.get("base"))]


def load_merged_profile_data(profile_path: Path, seen: set[Path] | None = None) -> Dict[str, Any]:
    profile_path = profile_path.resolve()
    seen = seen or set()
    if profile_path in seen:
        raise ValueError(f"Profile inheritance cycle detected at {profile_path}")
    seen.add(profile_path)
    try:
        with profile_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Profile {profile_path} must contain a mapping")

        merged: Dict[str, Any] = {}
        for parent in _parent_entries(data):
            parent_path = _resolve_parent_path(profile_path, parent)
            merged = deep_merge(merged, load_merged_profile_data(parent_path, seen=seen))
        return deep_merge(merged, data)
    finally:
        seen.remove(profile_path)
