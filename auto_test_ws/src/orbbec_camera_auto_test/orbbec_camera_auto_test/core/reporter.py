from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def append_log(path: Path, line: str) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line.rstrip("\n"))
        stream.write("\n")


def write_markdown(path: Path, lines: Iterable[str]) -> None:
    ensure_dir(path.parent)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _stringify_markdown_value(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return "N/A"
    return text.replace("|", "\\|").replace("\n", "<br>")


def _format_stream_config_summary(stream_config: List[Dict[str, Any]]) -> str:
    parts = []
    for stream in stream_config:
        label = stream.get("label") or stream.get("stream", "")
        width = stream.get("width", "")
        height = stream.get("height", "")
        fps = stream.get("fps", "")
        fmt = stream.get("format", "")
        resolution = f"{width}x{height}" if width or height else ""
        detail = " ".join(
            str(item)
            for item in (resolution, f"{fps}fps" if fps else "", fmt)
            if item
        )
        camera = stream.get("camera", "")
        name = f"{camera}/{label}" if camera else str(label)
        parts.append(f"{name}: {detail}" if detail else name)
    return "; ".join(parts)


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_stringify_markdown_value(value) for value in row) + " |")
    return lines


def _key_value_table(title: str, items: List[tuple[str, Any]]) -> List[str]:
    lines = [title, ""]
    lines.extend(_markdown_table(["Item", "Value"], [[key, value] for key, value in items]))
    lines.append("")
    return lines


def summarize_statuses(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for item in items:
        status = item.get("status", "failed")
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
    return counts


def build_functional_summary(result: Dict[str, Any]) -> List[str]:
    lines = [
        f"# Functional Test Summary: {result['profile_name']}",
        "",
    ]

    lines.extend(
        _key_value_table(
            "## Overview",
            [
                ("Status", result.get("status", "")),
                ("Camera name", result.get("camera_name", "")),
                ("Launch file", result.get("launch_file", "")),
            ],
        )
    )

    lines.append("## Launch Scenarios")
    lines.append("")
    scenario_rows: List[List[Any]] = []
    for scenario in result.get("scenarios", []):
        topic_counts = summarize_statuses(scenario.get("topics", []))
        service_counts = summarize_statuses(scenario.get("services", []))
        artifact_counts = summarize_statuses(scenario.get("artifacts", []))
        reboot = scenario.get("reboot", {"status": "skipped"})
        scenario_rows.append(
            [
                scenario.get("name", ""),
                scenario.get("status", ""),
                json.dumps(topic_counts, ensure_ascii=False, sort_keys=True),
                json.dumps(service_counts, ensure_ascii=False, sort_keys=True),
                json.dumps(artifact_counts, ensure_ascii=False, sort_keys=True),
                reboot.get("status", "skipped"),
            ]
        )
    if scenario_rows:
        lines.extend(
            _markdown_table(
                ["Scenario", "Status", "Topics", "Services", "Artifacts", "Reboot"],
                scenario_rows,
            )
        )
    else:
        lines.append("| Scenario | Status | Topics | Services | Artifacts | Reboot |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        lines.append("| N/A | N/A | N/A | N/A | N/A | N/A |")
    lines.append("")

    lines.append("## Failures")
    lines.append("")
    failures = collect_failures(result)
    if not failures:
        lines.extend(_markdown_table(["Failure"], [["None"]]))
    else:
        lines.extend(_markdown_table(["Failure"], [[failure] for failure in failures]))
    return lines


def build_performance_summary(result: Dict[str, Any]) -> List[str]:
    if "scenarios" in result:
        lines = [f"# Performance Test Summary: {result['profile_name']}", ""]
        host_environment = result.get("environment", {}).get("host", {})
        lines.append("## Test Environment")
        lines.append("")
        lines.append(f"- OS: `{_stringify_markdown_value(host_environment.get('os'))}`")
        lines.append(f"- Kernel: `{_stringify_markdown_value(host_environment.get('kernel'))}`")
        lines.append(
            f"- Architecture: `{_stringify_markdown_value(host_environment.get('architecture'))}`"
        )
        lines.append(
            f"- Logical CPUs: `{_stringify_markdown_value(host_environment.get('logical_cpus'))}`"
        )
        lines.append(
            f"- Total memory (GB): `{_stringify_markdown_value(host_environment.get('total_memory_gb'))}`"
        )
        lines.append(
            f"- ROS distro: `{_stringify_markdown_value(host_environment.get('ros_distro'))}`"
        )
        lines.append("")
        lines.append("## Scenario Summary")
        lines.append("")
        scenario_rows: List[List[Any]] = []
        for scenario in result.get("scenarios", []):
            scenario_rows.append(
                [
                    scenario.get("scenario_name", ""),
                    scenario.get("status", ""),
                    scenario.get("duration_seconds", 0.0),
                    _format_stream_config_summary(scenario.get("stream_config", [])),
                    scenario.get("scenario_description", ""),
                    scenario.get("result_dir", ""),
                ]
            )
        if scenario_rows:
            lines.extend(
                _markdown_table(
                    [
                        "Scenario",
                        "Status",
                        "Duration Seconds",
                        "Stream Configuration",
                        "Description",
                        "Result Directory",
                    ],
                    scenario_rows,
                )
            )
        else:
            lines.extend(
                _markdown_table(
                    [
                        "Scenario",
                        "Status",
                        "Duration Seconds",
                        "Stream Configuration",
                        "Description",
                        "Result Directory",
                    ],
                    [["N/A", "N/A", "N/A", "N/A", "N/A", "N/A"]],
                )
            )
        return lines

    environment = result.get("environment", {})
    host_environment = environment.get("host", {})
    camera_environment = environment.get("camera", {})
    camera_environments = environment.get("cameras", {})
    resource_attribution = environment.get("resource_attribution", {})
    launch_args = camera_environment.get("launch_args", {})
    if not launch_args:
        launch_args = result.get("launch_args", {})

    lines = [
        f"# Performance Test Summary: {result['profile_name']}"
        + (
            f" ({result['scenario_name']})"
            if result.get("scenario_name")
            else ""
        ),
        "",
    ]

    lines.append("## Test Environment")
    lines.append("")
    lines.append(f"- OS: `{_stringify_markdown_value(host_environment.get('os'))}`")
    lines.append(f"- Kernel: `{_stringify_markdown_value(host_environment.get('kernel'))}`")
    lines.append(
        f"- Architecture: `{_stringify_markdown_value(host_environment.get('architecture'))}`"
    )
    lines.append(
        f"- Logical CPUs: `{_stringify_markdown_value(host_environment.get('logical_cpus'))}`"
    )
    lines.append(
        f"- Total memory (GB): `{_stringify_markdown_value(host_environment.get('total_memory_gb'))}`"
    )
    lines.append(
        f"- ROS distro: `{_stringify_markdown_value(host_environment.get('ros_distro'))}`"
    )
    if camera_environments:
        lines.append(f"- Cameras: `{_stringify_markdown_value(result.get('camera_names'))}`")
        lines.append(
            f"- Resource mode: `{_stringify_markdown_value(result.get('resource_mode'))}`"
        )
        if resource_attribution:
            per_camera = resource_attribution.get("per_camera_cpu_ram")
            lines.append(
                f"- Per-camera CPU/RAM: `{_stringify_markdown_value(per_camera)}`"
            )
            if not per_camera:
                lines.append(
                    "- Resource attribution note: cameras are loaded into a shared component container."
                )
    else:
        lines.append(
            f"- Camera model: `{_stringify_markdown_value(camera_environment.get('camera_model'))}`"
        )
        lines.append(
            f"- ob_sdk_version: `{_stringify_markdown_value(camera_environment.get('ob_sdk_version'))}`"
        )
        lines.append(
            f"- ros_sdk_version: `{_stringify_markdown_value(camera_environment.get('ros_sdk_version'))}`"
        )
        lines.append(
            f"- Firmware version: `{_stringify_markdown_value(camera_environment.get('firmware_version'))}`"
        )
    lines.append("")

    if camera_environments:
        lines.append("## Cameras")
        lines.append("")
        camera_rows: List[List[Any]] = []
        for camera_name, camera_payload in camera_environments.items():
            camera_rows.append(
                [
                    camera_name,
                    camera_payload.get("camera_model", ""),
                    camera_payload.get("serial_number", ""),
                    camera_payload.get("firmware_version", ""),
                    camera_payload.get("ob_sdk_version", ""),
                    camera_payload.get("ros_sdk_version", ""),
                ]
            )
        lines.extend(
            _markdown_table(
                [
                    "Camera",
                    "Model",
                    "Serial Number",
                    "Firmware",
                    "OB SDK",
                    "ROS SDK",
                ],
                camera_rows,
            )
        )
        lines.append("")

    if launch_args:
        lines.append("## Camera Configuration")
        lines.append("")
        for key in sorted(launch_args):
            lines.append(f"- {key}: `{_stringify_markdown_value(launch_args[key])}`")
        lines.append(
            f"- Other parameters: keep the default values defined by the startup launch `{_stringify_markdown_value(result.get('launch_file'))}`"
        )
        lines.append("")

    stream_config = result.get("stream_config", [])
    if stream_config:
        lines.append("## Stream Configuration")
        lines.append("")
        stream_rows: List[List[Any]] = []
        for stream in stream_config:
            stream_rows.append(
                [
                    stream.get("camera", ""),
                    stream.get("label") or stream.get("stream", ""),
                    stream.get("width", ""),
                    stream.get("height", ""),
                    stream.get("fps", ""),
                    stream.get("format", ""),
                ]
            )
        lines.extend(
            _markdown_table(
                ["Camera", "Stream", "Width", "Height", "FPS", "Format"],
                stream_rows,
            )
        )
        lines.append("")

    frame_timestamps = result.get("frame_timestamps", {})
    if frame_timestamps:
        lines.append("## Frame Timestamps")
        lines.append("")
        lines.append(
            f"- Receiver recording: `{_stringify_markdown_value(frame_timestamps.get('enabled'))}`"
        )
        if frame_timestamps.get("enabled"):
            lines.append(
                f"- Receiver output directory: `{_stringify_markdown_value(frame_timestamps.get('output_dir'))}`"
            )
            lines.append(
                f"- Flush every rows: `{_stringify_markdown_value(frame_timestamps.get('flush_every_rows'))}`"
            )
        if frame_timestamps.get("driver_csv"):
            lines.append(
                f"- Driver CSV: `{_stringify_markdown_value(frame_timestamps.get('driver_csv'))}`"
            )
        lines.append("")

    lines.append("## Topic FPS")
    lines.append("")
    fps_rows: List[List[Any]] = []
    for topic_name, topic_summary in result.get("fps_summary", {}).items():
        fps_rows.append(
            [
                topic_name,
                f"{topic_summary.get('ideal_fps', 0.0):.2f}",
                f"{topic_summary.get('current_fps', 0.0):.2f}",
                f"{topic_summary.get('avg_fps', 0.0):.2f}",
                topic_summary.get("dropped_frames", 0),
                f"{topic_summary.get('drop_rate', 0.0) * 100.0:.3f}%",
            ]
        )
    if fps_rows:
        lines.extend(
            _markdown_table(
                ["Topic", "Ideal FPS", "Current FPS", "Average FPS", "Dropped Frames", "Drop Rate"],
                fps_rows,
            )
        )
    else:
        lines.extend(
            _markdown_table(
                ["Topic", "Ideal FPS", "Current FPS", "Average FPS", "Dropped Frames", "Drop Rate"],
                [["N/A", "N/A", "N/A", "N/A", "N/A", "N/A"]],
            )
        )
    lines.append("")

    system = result.get("system_summary", {})
    lines.append("## Camera Process Tree Usage")
    lines.append("")
    if any(isinstance(value, dict) for value in system.values()):
        system_rows: List[List[Any]] = []
        for scope_name, payload in system.items():
            system_rows.append(
                [
                    scope_name,
                    f"{payload.get('avg_cpu_percent', 0.0):.2f}",
                    f"{payload.get('max_cpu_percent', 0.0):.2f}",
                    f"{payload.get('avg_memory_rss_mb', 0.0):.2f}",
                    f"{payload.get('max_memory_rss_mb', 0.0):.2f}",
                ]
            )
        lines.extend(
            _markdown_table(
                [
                    "Scope",
                    "Avg CPU (%)",
                    "Max CPU (%)",
                    "Avg RAM (MB)",
                    "Max RAM (MB)",
                ],
                system_rows,
            )
        )
    else:
        lines.extend(
            _markdown_table(
                ["Metric", "Average", "Minimum", "Maximum"],
                [
                    [
                        "CPU Usage (%)",
                        f"{system.get('avg_cpu_percent', 0.0):.2f}",
                        f"{system.get('min_cpu_percent', 0.0):.2f}",
                        f"{system.get('max_cpu_percent', 0.0):.2f}",
                    ],
                    [
                        "RAM Usage (MB)",
                        f"{system.get('avg_memory_rss_mb', 0.0):.2f}",
                        f"{system.get('min_memory_rss_mb', 0.0):.2f}",
                        f"{system.get('max_memory_rss_mb', 0.0):.2f}",
                    ],
                ],
            )
        )
    return lines


def collect_failures(result: Dict[str, Any]) -> List[str]:
    failures: List[str] = []
    if result.get("preflight_error"):
        failures.append(f"preflight: {result['preflight_error']}")
    for scenario in result.get("scenarios", []):
        for item in (
            scenario.get("topics", [])
            + scenario.get("services", [])
            + scenario.get("artifacts", [])
        ):
            if item.get("status") == "failed":
                failures.append(
                    f"{scenario['name']}::{item['name']}: {item.get('message', 'failed')}"
                )
        reboot = scenario.get("reboot", {})
        if reboot.get("status") == "failed":
            failures.append(
                f"{scenario['name']}::reboot: {reboot.get('message', 'failed')}"
            )
    return failures
