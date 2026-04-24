from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .functional_services import (
    partition_service_specs,
    run_artifact_service_checks,
    run_reboot_check,
    run_service_checks,
)
from .functional_topics import run_topic_checks
from .profile_loader import CameraProfile, LaunchScenarioSpec, load_camera_profile
from .reporter import build_functional_summary, collect_failures, ensure_dir, write_json, write_markdown
from .ros_utils import RosHarness, resolve_service_type
from .session import TestSession, discover_orbbec_devices


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_launch_args(raw_args) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for raw in raw_args or []:
        if "=" not in raw:
            raise ValueError(f"Launch override must be KEY=VALUE, got: {raw}")
        key, value = raw.split("=", 1)
        parsed[key] = _parse_scalar(value)
    return parsed


def _build_launch_args(profile: CameraProfile, args) -> Dict[str, Any]:
    launch_args = dict(profile.default_launch_args)
    if args.camera_name:
        launch_args["camera_name"] = args.camera_name
    if args.serial_number:
        launch_args["serial_number"] = args.serial_number
    if args.usb_port:
        launch_args["usb_port"] = args.usb_port
    if args.config_file_path:
        launch_args["config_file_path"] = args.config_file_path
    launch_args.update(_parse_launch_args(args.launch_arg))
    return launch_args


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


def _require_detected_camera(driver_setup: str | None, emit_status) -> Dict[str, Any]:
    emit_status("probing connected Orbbec devices before launch")
    discovery = discover_orbbec_devices(driver_setup)
    if discovery["device_count"] > 0:
        emit_status(f"camera discovery succeeded: {discovery['message']}")
    elif discovery["success"]:
        emit_status("camera discovery finished: no camera found")
    else:
        emit_status(f"camera discovery failed: {discovery['message']}")

    if discovery.get("output"):
        for line in discovery["output"].splitlines():
            emit_status(f"discovery> {line}")

    if not discovery["success"]:
        raise RuntimeError(f"camera discovery command failed: {discovery['message']}")
    if discovery["device_count"] <= 0:
        raise RuntimeError("no Orbbec camera detected, aborting before launch")
    return discovery


def _run_scenario(
    harness: RosHarness,
    profile: CameraProfile,
    launch_file: str,
    scenario: LaunchScenarioSpec,
    base_launch_args: Dict[str, Any],
    results_dir: Path,
    launch_log_path: Path,
    topic_log_path: Path,
    service_log_path: Path,
    driver_setup: str | None,
    emit_status,
) -> tuple[Dict[str, Any], Any]:
    scenario_dir = ensure_dir(results_dir / "scenarios" / scenario.name)
    artifacts_dir = ensure_dir(scenario_dir / "artifacts")
    launch_args = dict(base_launch_args)
    launch_args.update(scenario.launch_args)
    write_json(
        scenario_dir / "launch_args.json",
        {"launch_file": launch_file, "launch_args": launch_args},
    )

    session = TestSession(
        launch_file=launch_file,
        launch_args=launch_args,
        work_dir=artifacts_dir,
        log_path=launch_log_path,
        driver_setup=driver_setup,
        status_callback=emit_status,
    )
    regular_services, artifact_services, reboot_service = partition_service_specs(scenario.services)

    scenario_result = {
        "name": scenario.name,
        "launch_args": launch_args,
        "graph_snapshot": {},
        "topics": [],
        "services": [],
        "artifacts": [],
        "reboot": {"status": "skipped", "message": "reboot service not configured"},
        "status": "passed",
        "message": "",
    }
    try:
        emit_status(f"starting launch scenario '{scenario.name}'")
        session.start()
        _wait_for_camera_ready(session, harness, launch_args["camera_name"], emit_status)
        emit_status(f"collecting ROS graph snapshot for scenario '{scenario.name}'")
        scenario_result["graph_snapshot"] = harness.graph_snapshot()
        emit_status(f"testing scenario topics for '{scenario.name}'")
        scenario_result["topics"] = run_topic_checks(
            harness, scenario.topics, topic_log_path, emit_status=emit_status
        )
        emit_status(f"testing scenario services for '{scenario.name}'")
        scenario_result["services"] = run_service_checks(
            harness, regular_services, service_log_path, emit_status=emit_status
        )
        emit_status(f"testing scenario artifact services for '{scenario.name}'")
        scenario_result["artifacts"] = run_artifact_service_checks(
            harness,
            artifact_services,
            artifacts_dir,
            service_log_path,
            emit_status=emit_status,
        )
    except Exception as exc:  # noqa: BLE001
        scenario_result["status"] = "failed"
        scenario_result["message"] = str(exc)
        emit_status(f"launch scenario '{scenario.name}' failed: {exc}")
    finally:
        session.stop()

    if any(
        item.get("status") == "failed"
        for item in (
            scenario_result["topics"]
            + scenario_result["services"]
            + scenario_result["artifacts"]
        )
    ):
        scenario_result["status"] = "failed"
    write_json(scenario_dir / "result.json", scenario_result)
    return scenario_result, reboot_service


def run_functional_test(args) -> int:
    results_dir = ensure_dir(Path(args.results_dir).resolve())
    scenarios_root = ensure_dir(results_dir / "scenarios")
    _ = scenarios_root

    launch_log_path = results_dir / "launch.log"
    topic_log_path = results_dir / "topic.log"
    service_log_path = results_dir / "service.log"
    stage_log_path = results_dir / "functional.log"
    emit_status = _make_status_logger(stage_log_path)

    emit_status(f"loading functional profile '{args.profile}'")
    profile = load_camera_profile(args.profile, profile_type="functional")
    launch_file = args.launch_file or profile.launch_file
    base_launch_args = _build_launch_args(profile, args)
    camera_name = str(base_launch_args.get("camera_name", "camera"))
    emit_status(f"functional test target launch: {launch_file}")
    emit_status(f"functional test camera name: {camera_name}")

    write_json(
        results_dir / "launch_args.json",
        {"launch_file": launch_file, "launch_args": base_launch_args},
    )

    result = {
        "profile_name": profile.profile_name,
        "launch_file": launch_file,
        "camera_name": camera_name,
        "status": "passed",
        "scenarios": [],
    }

    with RosHarness("orbbec_camera_functional_test") as harness:
        try:
            _require_detected_camera(args.driver_setup, emit_status)
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["preflight_error"] = str(exc)
            emit_status(f"functional preflight failed: {exc}")
            write_json(results_dir / "result.json", result)
            write_markdown(results_dir / "summary.md", build_functional_summary(result))
            return 1

        deferred_reboots = []
        emit_status("starting functional launch scenarios")
        for scenario in profile.launch_scenarios:
            scenario_result, reboot_spec = _run_scenario(
                harness=harness,
                profile=profile,
                launch_file=launch_file,
                scenario=scenario,
                base_launch_args=base_launch_args,
                results_dir=results_dir,
                launch_log_path=launch_log_path,
                topic_log_path=topic_log_path,
                service_log_path=service_log_path,
                driver_setup=args.driver_setup,
                emit_status=emit_status,
            )
            result["scenarios"].append(scenario_result)
            if reboot_spec is not None:
                deferred_reboots.append((scenario, scenario_result, reboot_spec))

        if deferred_reboots:
            emit_status("starting final reboot recovery checks")
        for scenario, scenario_result, reboot_spec in deferred_reboots:
            emit_status(f"running reboot as final step for scenario '{scenario.name}'")
            reboot_dir = ensure_dir(results_dir / "scenarios" / scenario.name / "reboot")
            launch_args = dict(base_launch_args)
            launch_args.update(scenario.launch_args)
            write_json(
                reboot_dir / "launch_args.json",
                {"launch_file": launch_file, "launch_args": launch_args},
            )
            reboot_session = TestSession(
                launch_file=launch_file,
                launch_args=launch_args,
                work_dir=reboot_dir,
                log_path=launch_log_path,
                driver_setup=args.driver_setup,
                status_callback=emit_status,
            )
            try:
                reboot_session.start()
                _wait_for_camera_ready(reboot_session, harness, launch_args["camera_name"], emit_status)
                image_topics = [topic for topic in scenario.topics if topic.validator == "image"]
                topic_names = ", ".join(topic.name for topic in image_topics) or "<none>"
                emit_status(
                    f"calling reboot service and waiting for image streams: {topic_names}"
                )
                scenario_result["reboot"] = run_reboot_check(
                    harness,
                    reboot_spec,
                    image_topics,
                    launch_args["camera_name"],
                    service_log_path,
                    emit_status=emit_status,
                )
            except Exception as exc:  # noqa: BLE001
                scenario_result["reboot"] = {"status": "failed", "message": str(exc)}
                emit_status(f"reboot recovery check failed for '{scenario.name}': {exc}")
            finally:
                reboot_session.stop()

            if scenario_result["reboot"].get("status") == "failed":
                scenario_result["status"] = "failed"
            write_json(results_dir / "scenarios" / scenario.name / "result.json", scenario_result)

    if collect_failures(result):
        result["status"] = "failed"
        emit_status("functional test finished with failures")
    else:
        emit_status("functional test finished successfully")
    write_json(results_dir / "result.json", result)
    write_markdown(results_dir / "summary.md", build_functional_summary(result))
    return 0 if result["status"] == "passed" else 1


def parse_args():
    parser = argparse.ArgumentParser(description="Run Orbbec camera functional tests")
    parser.add_argument("--profile", default="gemini_330_series", help="Profile name or YAML path")
    parser.add_argument("--launch-file", default="", help="Override launch file from the profile")
    parser.add_argument("--camera-name", default=None)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--usb-port", default=None)
    parser.add_argument("--config-file-path", default=None)
    parser.add_argument("--driver-setup", default=None)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra KEY=VALUE launch arg")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        return_code = run_functional_test(args)
    except KeyboardInterrupt:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] functional test interrupted by user", flush=True)
        return_code = 130
    sys.exit(return_code)


if __name__ == "__main__":
    main()
