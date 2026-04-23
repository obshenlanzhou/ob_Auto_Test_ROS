from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .functional_runner import _build_launch_args, _require_detected_camera
from .environment_info import collect_camera_environment, collect_host_environment
from .functional_topics import run_topic_checks
from .performance_fps import TopicFpsCollector
from .performance_load import ExternalLoadController
from .performance_system import MultiCameraSystemSampler, ProcessTreeSampler
from .profile_loader import PerformanceScenarioSpec, TopicSpec, load_camera_profile
from .reporter import append_log, build_performance_summary, ensure_dir, write_json, write_markdown
from .ros_utils import RosHarness, resolve_service_type
from .session import TestSession


def _parse_duration_value(value: Any) -> float:
    if value is None:
        raise ValueError("duration is required")
    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip().lower()
    if not raw:
        raise ValueError("duration is required")

    multiplier = 1.0
    if raw.endswith("s"):
        raw = raw[:-1]
    elif raw.endswith("m"):
        raw = raw[:-1]
        multiplier = 60.0
    elif raw.endswith("h"):
        raw = raw[:-1]
        multiplier = 3600.0
    elif raw.endswith("d"):
        raw = raw[:-1]
        multiplier = 86400.0

    duration_seconds = float(raw) * multiplier
    if duration_seconds <= 0.0:
        raise ValueError("duration must be > 0")
    return duration_seconds


def _format_hms(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_duration_cn(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0 or parts:
        parts.append(f"{hours}小时")
    if minutes > 0 or parts:
        parts.append(f"{minutes}分钟")
    parts.append(f"{secs}秒")
    return "".join(parts)


def _make_status_logger(*log_paths: Path):
    def emit(message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        for log_path in log_paths:
            write_path = Path(log_path)
            write_path.parent.mkdir(parents=True, exist_ok=True)
            with write_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    return emit


def _wait_for_camera_ready(
    session: TestSession, harness: RosHarness, camera_name: str, emit_status
) -> None:
    session.assert_running()
    emit_status(f"waiting for camera node '/{camera_name}/{camera_name}'")
    harness.wait_for_node(camera_name, namespace=f"/{camera_name}", timeout=60.0)
    emit_status(f"camera node '/{camera_name}/{camera_name}' is online")
    emit_status(f"waiting for service '/{camera_name}/get_sdk_version'")
    harness.wait_for_service(
        f"/{camera_name}/get_sdk_version",
        resolve_service_type("orbbec_camera_msgs/srv/GetString"),
        timeout=60.0,
    )
    emit_status(f"launch is ready for camera '{camera_name}'")


def _wait_for_cameras_ready(
    session: TestSession, harness: RosHarness, camera_names: list[str], emit_status
) -> None:
    for camera_name in camera_names:
        _wait_for_camera_ready(session, harness, camera_name, emit_status)


def _scenario_duration(args, scenario: PerformanceScenarioSpec) -> float:
    if args.duration is not None:
        return _parse_duration_value(args.duration)
    if scenario.duration is not None:
        return _parse_duration_value(scenario.duration)
    raise ValueError(
        f"performance scenario '{scenario.name}' has no duration; pass --duration or set duration in profile"
    )


def _select_performance_scenarios(profile, scenario_name: str | None) -> list[PerformanceScenarioSpec]:
    scenarios = list(profile.performance_scenarios)
    if not scenario_name:
        return scenarios

    for scenario in scenarios:
        if scenario.name == scenario_name:
            return [scenario]
    available = ", ".join(item.name for item in scenarios)
    raise ValueError(
        f"unknown performance scenario '{scenario_name}'. available scenarios: {available}"
    )


def _scenario_result_payload(
    profile_name: str,
    launch_file: str,
    camera_name: str,
    scenario: PerformanceScenarioSpec,
    launch_args: Dict[str, Any],
    duration_seconds: float,
    host_environment: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "profile_name": profile_name,
        "scenario_name": scenario.name,
        "scenario_description": scenario.description,
        "launch_file": launch_file,
        "camera_name": camera_name,
        "duration_seconds": duration_seconds,
        "launch_args": dict(launch_args),
        "load": asdict(scenario.load) if scenario.load is not None else {},
        "status": "passed",
        "fps_summary": {},
        "system_summary": {},
        "environment": {
            "host": host_environment,
            "camera": {
                "camera_name": camera_name,
                "launch_file": launch_file,
                "launch_args": dict(launch_args),
            },
        },
    }


def _multi_camera_result_payload(
    profile_name: str,
    launch_file: str,
    camera_names: list[str],
    resource_mode: str,
    container_name: str,
    scenario: PerformanceScenarioSpec,
    launch_args: Dict[str, Any],
    duration_seconds: float,
    host_environment: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "profile_name": profile_name,
        "scenario_name": scenario.name,
        "scenario_description": scenario.description,
        "launch_file": launch_file,
        "camera_name": ",".join(camera_names),
        "camera_names": list(camera_names),
        "resource_mode": resource_mode,
        "container_name": container_name,
        "duration_seconds": duration_seconds,
        "launch_args": dict(launch_args),
        "load": asdict(scenario.load) if scenario.load is not None else {},
        "status": "passed",
        "fps_summary": {},
        "system_summary": {},
        "environment": {
            "host": host_environment,
            "cameras": {},
            "resource_attribution": {
                "mode": resource_mode,
                "per_camera_cpu_ram": resource_mode == "isolated_containers",
                "container_name": container_name,
            },
        },
    }


def _format_topic_name(template: str, camera_name: str) -> str:
    return (
        template.replace("{camera}", camera_name)
        .replace("{camera_name}", camera_name)
        .replace("${camera}", camera_name)
        .replace("${camera_name}", camera_name)
    )


def _expand_multi_camera_topics(profile, scenario: PerformanceScenarioSpec) -> list[TopicSpec]:
    multi_camera = profile.multi_camera
    template_specs = scenario.topics or multi_camera.topic_templates
    topics: list[TopicSpec] = []
    for camera_name in multi_camera.cameras:
        for spec in template_specs:
            topics.append(
                TopicSpec(
                    name=_format_topic_name(spec.name, camera_name),
                    type=spec.type,
                    mode=spec.mode,
                    validator=spec.validator,
                    paired_topic=(
                        _format_topic_name(spec.paired_topic, camera_name)
                        if spec.paired_topic
                        else None
                    ),
                    timeout=spec.timeout,
                    qos=spec.qos,
                    ideal_fps_key=spec.ideal_fps_key,
                    ideal_fps=spec.ideal_fps,
                )
            )
    return topics


def _result_exit_code(result: Dict[str, Any]) -> int:
    status = result.get("status")
    if status == "passed":
        return 0
    if status == "interrupted":
        return 130
    return 1


def _safe_harness_name(scenario_name: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in scenario_name)
    normalized = normalized.strip("_") or "default"
    return f"orbbec_camera_performance_{normalized}"


def _run_performance_scenario(
    *,
    args,
    profile,
    scenario: PerformanceScenarioSpec,
    launch_file: str,
    base_launch_args: Dict[str, Any],
    results_dir: Path,
    host_environment: Dict[str, Any],
) -> Dict[str, Any]:
    launch_log_path = results_dir / "launch.log"
    performance_log_path = results_dir / "performance.log"
    fps_csv_path = results_dir / "fps.csv"
    system_csv_path = results_dir / "system_usage.csv"
    stage_log_path = results_dir / "performance_stage.log"
    emit_status = _make_status_logger(stage_log_path, performance_log_path)

    launch_args = dict(base_launch_args)
    launch_args.update(scenario.launch_args)
    camera_name = str(launch_args.get("camera_name", "camera"))
    multi_camera = profile.multi_camera
    is_multi_camera = bool(multi_camera.enabled and multi_camera.cameras)
    camera_names = list(multi_camera.cameras) if is_multi_camera else [camera_name]
    performance_topics = (
        _expand_multi_camera_topics(profile, scenario) if is_multi_camera else scenario.topics
    )
    resource_mode = multi_camera.resource_mode if is_multi_camera else ""
    container_name = multi_camera.container_name if is_multi_camera else ""
    duration_seconds = _scenario_duration(args, scenario)
    emit_status(f"performance scenario '{scenario.name}' target launch: {launch_file}")
    if is_multi_camera:
        emit_status(
            f"performance scenario '{scenario.name}' cameras: {', '.join(camera_names)}"
        )
        emit_status(
            f"performance scenario '{scenario.name}' resource mode: {resource_mode}"
        )
    else:
        emit_status(f"performance scenario '{scenario.name}' camera name: {camera_name}")
    emit_status(
        f"performance scenario '{scenario.name}' duration: {_format_duration_cn(duration_seconds)}"
    )
    if scenario.description:
        emit_status(f"performance scenario '{scenario.name}' description: {scenario.description}")

    write_json(
        results_dir / "launch_args.json",
        {
            "scenario_name": scenario.name,
            "description": scenario.description,
            "launch_file": launch_file,
            "launch_args": launch_args,
            "camera_names": camera_names,
            "resource_mode": resource_mode,
            "container_name": container_name,
            "duration_seconds": duration_seconds,
            "load": asdict(scenario.load) if scenario.load is not None else {},
        },
    )

    if is_multi_camera:
        result = _multi_camera_result_payload(
            profile.profile_name,
            launch_file,
            camera_names,
            resource_mode,
            container_name,
            scenario,
            launch_args,
            duration_seconds,
            host_environment,
        )
    else:
        result = _scenario_result_payload(
            profile.profile_name,
            launch_file,
            camera_name,
            scenario,
            launch_args,
            duration_seconds,
            host_environment,
        )

    with RosHarness(_safe_harness_name(scenario.name)) as harness:
        try:
            _require_detected_camera(args.driver_setup, emit_status)
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["error"] = str(exc)
            emit_status(f"performance scenario '{scenario.name}' preflight failed: {exc}")
            write_json(results_dir / "result.json", result)
            write_markdown(results_dir / "summary.md", build_performance_summary(result))
            return result

        session = TestSession(
            launch_file=launch_file,
            launch_args=launch_args,
            work_dir=results_dir,
            log_path=launch_log_path,
            driver_setup=args.driver_setup,
            status_callback=emit_status,
        )
        collector = None
        sampler = None
        load_controller = ExternalLoadController(scenario.load, emit_status=emit_status)
        try:
            emit_status(f"starting clean performance launch for scenario '{scenario.name}'")
            session.start()
            if is_multi_camera:
                _wait_for_cameras_ready(session, harness, camera_names, emit_status)
                for current_camera in camera_names:
                    result["environment"]["cameras"][current_camera] = collect_camera_environment(
                        harness, current_camera, launch_file, launch_args
                    )
            else:
                _wait_for_camera_ready(session, harness, camera_name, emit_status)
                result["environment"]["camera"] = collect_camera_environment(
                    harness, camera_name, launch_file, launch_args
                )
            emit_status(f"warming up performance topics for scenario '{scenario.name}'")
            warmup_results = run_topic_checks(
                harness, performance_topics, performance_log_path, emit_status=emit_status
            )
            if any(item["status"] == "failed" for item in warmup_results):
                failed_topics = [item["name"] for item in warmup_results if item["status"] == "failed"]
                raise RuntimeError(f"Performance warmup failed for topics: {failed_topics}")

            emit_status(f"starting FPS collector for scenario '{scenario.name}'")
            collector = TopicFpsCollector(
                harness.node,
                performance_topics,
                fps_csv_path,
                launch_args=launch_args,
            )
            for line in collector.describe_topics():
                emit_status(line)
            if is_multi_camera:
                sampler = MultiCameraSystemSampler(
                    session,
                    system_csv_path,
                    camera_names=camera_names,
                    resource_mode=resource_mode,
                    container_name=container_name,
                )
            else:
                sampler = ProcessTreeSampler(session, system_csv_path)

            deadline = time.monotonic() + duration_seconds
            next_system_sample = time.monotonic()
            next_progress_log = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                harness.spin_once(0.1)
                session.assert_running()
                current_time = time.monotonic()
                elapsed = current_time - collector.start_time
                load_controller.update(elapsed, duration_seconds)
                if current_time >= next_system_sample:
                    snapshot = sampler.sample(elapsed)
                    if is_multi_camera:
                        parts = [
                            (
                                f"{scope}:{name} cpu={item['cpu_percent']:.2f} "
                                f"rss_mb={item['memory_rss_mb']:.2f}"
                            )
                            for scope, name, item in snapshot
                        ]
                        append_log(
                            performance_log_path,
                            f"[PERF] elapsed={elapsed:.2f}s " + "; ".join(parts),
                        )
                    else:
                        append_log(
                            performance_log_path,
                            f"[PERF] elapsed={elapsed:.2f}s cpu={snapshot['cpu_percent']:.2f} rss_mb={snapshot['memory_rss_mb']:.2f}",
                        )
                    next_system_sample = current_time + 1.0
                if current_time >= next_progress_log:
                    emit_status(f"performance elapsed: {_format_hms(elapsed)}")
                    next_progress_log = current_time + 10.0

            result["fps_summary"] = collector.build_summary()
            result["system_summary"] = sampler.build_summary()
            emit_status(f"performance scenario '{scenario.name}' sampling completed")
        except KeyboardInterrupt:
            result["status"] = "interrupted"
            result["error"] = "interrupted by user"
            append_log(performance_log_path, "[PERF] INTERRUPTED: interrupted by user")
            emit_status(
                f"performance scenario '{scenario.name}' interrupted by user, finalizing partial results"
            )
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["error"] = str(exc)
            append_log(performance_log_path, f"[PERF] FAIL: {exc}")
            emit_status(f"performance scenario '{scenario.name}' failed: {exc}")
        finally:
            total_elapsed = 0.0
            if collector is not None:
                total_elapsed = time.monotonic() - collector.start_time
            emit_status(f"total performance duration: {_format_duration_cn(total_elapsed)}")
            load_controller.close()
            if collector is not None:
                result["fps_summary"] = result["fps_summary"] or collector.build_summary()
                collector.close()
            if sampler is not None:
                result["system_summary"] = result["system_summary"] or sampler.build_summary()
                sampler.close()
            session.stop()

    write_json(results_dir / "result.json", result)
    write_markdown(results_dir / "summary.md", build_performance_summary(result))
    if result["status"] == "passed":
        emit_status(f"performance scenario '{scenario.name}' finished successfully")
    elif result["status"] == "interrupted":
        emit_status(f"performance scenario '{scenario.name}' wrote partial results after interruption")
    return result


def run_performance_test(args) -> int:
    results_dir = ensure_dir(Path(args.results_dir).resolve())
    root_stage_log_path = results_dir / "performance_stage.log"
    root_log_path = results_dir / "performance.log"
    emit_status = _make_status_logger(root_stage_log_path, root_log_path)

    emit_status(f"loading performance profile '{args.profile}'")
    profile = load_camera_profile(args.profile)
    launch_file = args.launch_file or profile.launch_file
    base_launch_args = _build_launch_args(profile, args)
    selected_scenarios = _select_performance_scenarios(profile, args.performance_scenario)
    host_environment = collect_host_environment(args.driver_setup)
    emit_status(f"selected performance scenarios: {', '.join(item.name for item in selected_scenarios)}")

    if len(selected_scenarios) == 1:
        result = _run_performance_scenario(
            args=args,
            profile=profile,
            scenario=selected_scenarios[0],
            launch_file=launch_file,
            base_launch_args=base_launch_args,
            results_dir=results_dir,
            host_environment=host_environment,
        )
        return _result_exit_code(result)

    aggregate_result = {
        "profile_name": profile.profile_name,
        "launch_file": launch_file,
        "camera_name": str(base_launch_args.get("camera_name", "camera")),
        "status": "passed",
        "selected_scenarios": [scenario.name for scenario in selected_scenarios],
        "environment": {"host": host_environment},
        "scenarios": [],
    }

    for scenario in selected_scenarios:
        scenario_dir = ensure_dir(results_dir / "scenarios" / scenario.name)
        scenario_result = _run_performance_scenario(
            args=args,
            profile=profile,
            scenario=scenario,
            launch_file=launch_file,
            base_launch_args=base_launch_args,
            results_dir=scenario_dir,
            host_environment=host_environment,
        )
        scenario_result["result_dir"] = str(scenario_dir.relative_to(results_dir))
        aggregate_result["scenarios"].append(scenario_result)
        if scenario_result["status"] == "interrupted":
            aggregate_result["status"] = "interrupted"
            break
        if scenario_result["status"] != "passed":
            aggregate_result["status"] = "failed"

    write_json(results_dir / "result.json", aggregate_result)
    write_markdown(results_dir / "summary.md", build_performance_summary(aggregate_result))
    if aggregate_result["status"] == "passed":
        emit_status("all selected performance scenarios finished successfully")
    elif aggregate_result["status"] == "interrupted":
        emit_status("performance test interrupted by user, partial results were written")
    else:
        emit_status("one or more performance scenarios failed")
    return _result_exit_code(aggregate_result)


def parse_args():
    parser = argparse.ArgumentParser(description="Run Orbbec camera performance tests")
    parser.add_argument("--profile", default="gemini_330_series", help="Profile name or YAML path")
    parser.add_argument("--launch-file", default="", help="Override launch file from the profile")
    parser.add_argument(
        "--performance-scenario",
        default="",
        help="Run only the named performance scenario from the profile",
    )
    parser.add_argument("--camera-name", default=None)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--usb-port", default=None)
    parser.add_argument("--config-file-path", default=None)
    parser.add_argument("--driver-setup", default=None)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument(
        "--duration",
        default=None,
        help="Override scenario duration, supports seconds or suffixes like 15m, 2h, 1d",
    )
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra KEY=VALUE launch arg")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    return_code = run_performance_test(args)
    sys.exit(return_code)


if __name__ == "__main__":
    main()
