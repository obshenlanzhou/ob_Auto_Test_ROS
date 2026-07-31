from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from ..checks.services import (
    partition_service_specs,
    run_artifact_service_checks,
    run_reboot_check,
    run_service_checks,
    select_discovered_service_specs,
)
from ..checks.topics import run_topic_checks, select_discovered_topic_specs
from ..core.reporter import build_functional_summary, collect_failures, ensure_dir, write_json, write_markdown
from ..core.ros_utils import RosHarness, resolve_service_type
from ..core.session import TestSession, discover_orbbec_devices
from ..profile.loader import CameraProfile, LaunchScenarioSpec, load_camera_profile
from ..profile.requirements import (
    LaunchRequirementProfile,
    ResolvedInterfaceRequirements,
    load_launch_requirement_profile,
    resolve_required_interfaces,
)
from ..profile.templating import expand_launch_scenario


REQUIRED_INTERFACE_DISCOVERY_TIMEOUT = 10.0
REQUIRED_INTERFACE_DISCOVERY_INTERVAL = 0.2


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
    session.assert_healthy()
    emit_status(f"waiting for camera node '/{camera_name}/{camera_name}'")
    harness.wait_for_node(camera_name, namespace=f"/{camera_name}", timeout=60.0)
    session.assert_healthy()
    emit_status(f"camera node '/{camera_name}/{camera_name}' is online")
    emit_status(f"waiting for service '/{camera_name}/get_sdk_version'")
    try:
        harness.wait_for_service(
            f"/{camera_name}/get_sdk_version",
            resolve_service_type(
                "orbbec_camera_msgs/srv/GetString", harness.ros_version
            ),
            timeout=60.0,
        )
    except Exception as exc:  # noqa: BLE001
        session.assert_healthy()
        emit_status(
            "readiness service is unavailable; continuing to required-interface "
            f"validation: {exc}"
        )
    session.assert_healthy()
    emit_status(f"launch is ready for camera '{camera_name}'")


def _require_detected_camera(
    driver_setup: str | None,
    emit_status,
    ros_version: str = "2",
    ros_setup: str | None = None,
) -> Dict[str, Any]:
    emit_status("probing connected Orbbec devices before launch")
    discovery = discover_orbbec_devices(driver_setup, ros_version=ros_version, ros_setup=ros_setup)
    if discovery.get("skipped"):
        emit_status(f"camera discovery skipped: {discovery['message']}")
    elif discovery["device_count"] > 0:
        emit_status(f"camera discovery succeeded: {discovery['message']}")
    elif discovery["success"]:
        emit_status("camera discovery finished: no camera found")
    else:
        emit_status(f"camera discovery failed: {discovery['message']}")

    if discovery.get("output"):
        for line in discovery["output"].splitlines():
            emit_status(f"discovery> {line}")

    if discovery.get("skipped"):
        return discovery
    if not discovery["success"]:
        raise RuntimeError(f"camera discovery command failed: {discovery['message']}")
    if discovery["device_count"] <= 0:
        raise RuntimeError("no Orbbec camera detected, aborting before launch")
    return discovery


def _required_topic_unavailable_reason(spec, discovered_names: set[str]) -> str:
    if spec.name not in discovered_names:
        return f"required topic not advertised: {spec.name}"
    if spec.paired_topic and spec.paired_topic not in discovered_names:
        return (
            f"required paired topic not advertised: {spec.paired_topic} "
            f"(required by {spec.name})"
        )
    return ""


def _required_service_unavailable_reason(spec, discovered_names: set[str]) -> str:
    if spec.name not in discovered_names:
        return f"required service not advertised: {spec.name}"
    if spec.getter_name and spec.getter_name not in discovered_names:
        return (
            f"required getter service not advertised: {spec.getter_name} "
            f"(required by {spec.name})"
        )
    return ""


def _required_topic_failure(spec, reason: str, profile_name: str) -> Dict[str, Any]:
    return {
        "name": spec.name,
        "type": spec.type,
        "mode": spec.mode,
        "validator": spec.validator,
        "required": True,
        "status": "failed",
        "message": f"{reason}; requirement profile: {profile_name}",
    }


def _required_service_failure(spec, reason: str, profile_name: str) -> Dict[str, Any]:
    return {
        "name": spec.name,
        "type": spec.type,
        "mode": spec.mode,
        "required": True,
        "status": "failed",
        "message": f"{reason}; requirement profile: {profile_name}",
    }


def _validate_requirement_catalog(
    scenario: LaunchScenarioSpec,
    requirements: ResolvedInterfaceRequirements,
) -> None:
    catalog_topics = {spec.name for spec in scenario.topics}
    catalog_services = {spec.name for spec in scenario.services}
    unknown_topics = sorted(set(requirements.required_topics) - catalog_topics)
    unknown_services = sorted(set(requirements.required_services) - catalog_services)
    if unknown_topics or unknown_services:
        details = []
        if unknown_topics:
            details.append(f"topics={unknown_topics}")
        if unknown_services:
            details.append(f"services={unknown_services}")
        raise ValueError(
            "required-interface table references interfaces missing from the "
            f"functional catalog: {', '.join(details)}"
        )


def _set_required_flags(
    results: list[Dict[str, Any]], required_names: set[str]
) -> None:
    for item in results:
        item["required"] = item.get("name") in required_names


def _required_interface_gaps(
    scenario: LaunchScenarioSpec,
    requirements: ResolvedInterfaceRequirements,
    graph_snapshot: Dict[str, Any],
) -> tuple[Dict[str, str], Dict[str, str]]:
    discovered_topic_names = {
        item.get("name") for item in graph_snapshot.get("topics", [])
    }
    discovered_service_names = {
        item.get("name") for item in graph_snapshot.get("services", [])
    }
    topic_specs_by_name = {spec.name: spec for spec in scenario.topics}
    service_specs_by_name = {spec.name: spec for spec in scenario.services}
    missing_topic_reasons = {
        name: reason
        for name in requirements.required_topics
        if (
            reason := _required_topic_unavailable_reason(
                topic_specs_by_name[name], discovered_topic_names
            )
        )
    }
    missing_service_reasons = {
        name: reason
        for name in requirements.required_services
        if (
            reason := _required_service_unavailable_reason(
                service_specs_by_name[name], discovered_service_names
            )
        )
    }
    return missing_topic_reasons, missing_service_reasons


def _wait_for_required_interfaces(
    session: TestSession,
    harness: RosHarness,
    scenario: LaunchScenarioSpec,
    requirements: ResolvedInterfaceRequirements,
    emit_status,
    timeout: float = REQUIRED_INTERFACE_DISCOVERY_TIMEOUT,
) -> tuple[Dict[str, Any], Dict[str, str], Dict[str, str]]:
    deadline = time.monotonic() + max(timeout, 0.0)
    last_missing_names: tuple[str, ...] | None = None
    while True:
        session.assert_healthy()
        graph_snapshot = harness.graph_snapshot()
        session.assert_healthy()
        missing_topic_reasons, missing_service_reasons = (
            _required_interface_gaps(
                scenario,
                requirements,
                graph_snapshot,
            )
        )
        missing_names = tuple(
            [*missing_topic_reasons, *missing_service_reasons]
        )
        if not missing_names:
            if last_missing_names:
                emit_status("all required ROS interfaces are now available")
            return graph_snapshot, {}, {}
        if time.monotonic() >= deadline:
            return (
                graph_snapshot,
                missing_topic_reasons,
                missing_service_reasons,
            )
        if missing_names != last_missing_names:
            emit_status(
                "waiting for required ROS interfaces: "
                + ", ".join(missing_names)
            )
            last_missing_names = missing_names
        harness.spin_once(REQUIRED_INTERFACE_DISCOVERY_INTERVAL)


def _run_scenario(
    profile: CameraProfile,
    requirement_profile: LaunchRequirementProfile,
    launch_file: str,
    scenario: LaunchScenarioSpec,
    base_launch_args: Dict[str, Any],
    results_dir: Path,
    launch_log_path: Path,
    topic_log_path: Path,
    service_log_path: Path,
    driver_setup: str | None,
    ros_version: str,
    ros_setup: str | None,
    emit_status,
) -> tuple[Dict[str, Any], Any]:
    scenario_dir = ensure_dir(results_dir / "scenarios" / scenario.name)
    artifacts_dir = ensure_dir(scenario_dir / "artifacts")
    launch_args = dict(base_launch_args)
    launch_args.update(scenario.launch_args)
    camera_name = str(launch_args.get("camera_name", "camera"))
    scenario = expand_launch_scenario(scenario, camera_name)
    requirements = resolve_required_interfaces(
        requirement_profile, launch_args, camera_name
    )
    required_topic_names = set(requirements.required_topics)
    required_service_names = set(requirements.required_services)
    _validate_requirement_catalog(scenario, requirements)
    write_json(
        scenario_dir / "launch_args.json",
        {
            "launch_file": launch_file,
            "launch_args": launch_args,
            "effective_launch_args": requirements.effective_launch_args,
        },
    )

    session = TestSession(
        launch_file=launch_file,
        launch_args=launch_args,
        work_dir=artifacts_dir,
        log_path=launch_log_path,
        driver_setup=driver_setup,
        ros_version=ros_version,
        ros_setup=ros_setup,
        status_callback=emit_status,
    )
    regular_services, artifact_services, reboot_service = partition_service_specs(scenario.services)

    scenario_result = {
        "name": scenario.name,
        "launch_args": launch_args,
        "requirements": {
            "profile_name": requirements.profile_name,
            "launch_file": requirement_profile.launch_file,
            "effective_launch_args": requirements.effective_launch_args,
            "matched_rules": requirements.matched_rules,
            "required_topics": requirements.required_topics,
            "required_services": requirements.required_services,
            "missing_topics": [],
            "missing_services": [],
            "status": "not_checked",
        },
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
        with RosHarness("orbbec_camera_functional_test", ros_version=ros_version) as harness:
            _wait_for_camera_ready(session, harness, camera_name, emit_status)
            emit_status(
                f"waiting up to {REQUIRED_INTERFACE_DISCOVERY_TIMEOUT:.0f}s "
                f"for required ROS interfaces in scenario '{scenario.name}'"
            )
            (
                scenario_result["graph_snapshot"],
                missing_topic_reasons,
                missing_service_reasons,
            ) = _wait_for_required_interfaces(
                session,
                harness,
                scenario,
                requirements,
                emit_status,
                timeout=REQUIRED_INTERFACE_DISCOVERY_TIMEOUT,
            )
            discovered_topic_names = {
                item.get("name") for item in scenario_result["graph_snapshot"].get("topics", [])
            }
            discovered_service_names = {
                item.get("name") for item in scenario_result["graph_snapshot"].get("services", [])
            }
            topic_specs_by_name = {spec.name: spec for spec in scenario.topics}
            service_specs_by_name = {spec.name: spec for spec in scenario.services}
            scenario_result["requirements"].update(
                {
                    "missing_topics": list(missing_topic_reasons),
                    "missing_services": list(missing_service_reasons),
                    "status": (
                        "failed"
                        if missing_topic_reasons or missing_service_reasons
                        else "passed"
                    ),
                }
            )
            if missing_topic_reasons or missing_service_reasons:
                scenario_result["topics"].extend(
                    _required_topic_failure(
                        topic_specs_by_name[name],
                        reason,
                        requirements.profile_name,
                    )
                    for name, reason in missing_topic_reasons.items()
                )
                for name, reason in missing_service_reasons.items():
                    spec = service_specs_by_name[name]
                    failure = _required_service_failure(
                        spec, reason, requirements.profile_name
                    )
                    if spec.mode == "artifact":
                        scenario_result["artifacts"].append(failure)
                    elif spec.mode == "reboot":
                        scenario_result["reboot"] = failure
                    else:
                        scenario_result["services"].append(failure)
                missing_names = [
                    *missing_topic_reasons,
                    *missing_service_reasons,
                ]
                raise RuntimeError(
                    "required-interface validation failed; stopping scenario "
                    f"before functional checks: {', '.join(missing_names)}"
                )
            discovered_topics = select_discovered_topic_specs(
                scenario.topics, discovered_topic_names
            )
            regular_services = select_discovered_service_specs(
                regular_services, discovered_service_names
            )
            artifact_services = select_discovered_service_specs(
                artifact_services, discovered_service_names
            )
            artifact_services = [
                replace(
                    spec,
                    keepalive_topics=select_discovered_topic_specs(
                        spec.keepalive_topics, discovered_topic_names
                    ),
                )
                for spec in artifact_services
            ]
            if reboot_service is not None and (
                reboot_service.name not in discovered_service_names
            ):
                reason = missing_service_reasons.get(reboot_service.name)
                if reason:
                    scenario_result["reboot"] = _required_service_failure(
                        reboot_service, reason, requirements.profile_name
                    )
                else:
                    scenario_result["reboot"] = {
                        "name": reboot_service.name,
                        "type": reboot_service.type,
                        "required": False,
                        "status": "skipped",
                        "message": "reboot service not discovered",
                    }
                reboot_service = None
            emit_status(
                f"topic discovery selected {len(discovered_topics)}/{len(scenario.topics)} interfaces"
            )
            emit_status(
                "service discovery selected "
                f"{len(regular_services) + len(artifact_services) + int(reboot_service is not None)}"
                f"/{len(scenario.services)} interfaces"
            )
            emit_status(f"testing scenario topics for '{scenario.name}'")
            scenario_result["topics"] = run_topic_checks(
                harness, discovered_topics, topic_log_path, emit_status=emit_status
            )
            _set_required_flags(
                scenario_result["topics"], required_topic_names
            )
            emit_status(f"testing scenario services for '{scenario.name}'")
            scenario_result["services"] = run_service_checks(
                harness, regular_services, service_log_path, emit_status=emit_status
            )
            _set_required_flags(
                scenario_result["services"], required_service_names
            )
            emit_status(f"testing scenario artifact services for '{scenario.name}'")
            scenario_result["artifacts"] = run_artifact_service_checks(
                harness,
                artifact_services,
                artifacts_dir,
                service_log_path,
                emit_status=emit_status,
            )
            _set_required_flags(
                scenario_result["artifacts"], required_service_names
            )
    except Exception as exc:  # noqa: BLE001
        scenario_result["status"] = "failed"
        scenario_result["message"] = str(exc)
        reboot_service = None
        emit_status(f"launch scenario '{scenario.name}' failed: {exc}")
    finally:
        session.stop()

    if (
        scenario_result["requirements"].get("status") == "failed"
        or scenario_result["reboot"].get("status") == "failed"
        or any(
            item.get("status") == "failed"
            for item in (
                scenario_result["topics"]
                + scenario_result["services"]
                + scenario_result["artifacts"]
            )
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

    emit_status("loading the generic functional interface catalog")
    profile = load_camera_profile("all_topics_services", profile_type="functional")
    launch_file = args.launch_file
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
        "ros_version": str(args.ros_version),
        "launch_file": launch_file,
        "camera_name": camera_name,
        "status": "passed",
        "scenarios": [],
    }

    try:
        requirement_profile = load_launch_requirement_profile(
            launch_file, args.ros_version
        )
        for raw_scenario in profile.launch_scenarios:
            validation_args = dict(base_launch_args)
            validation_args.update(raw_scenario.launch_args)
            validation_camera = str(validation_args.get("camera_name", "camera"))
            validation_scenario = expand_launch_scenario(
                raw_scenario, validation_camera
            )
            validation_requirements = resolve_required_interfaces(
                requirement_profile, validation_args, validation_camera
            )
            _validate_requirement_catalog(
                validation_scenario, validation_requirements
            )
        result["requirement_profile"] = {
            "name": requirement_profile.name,
            "launch_file": requirement_profile.launch_file,
        }
        emit_status(
            "matched required-interface profile "
            f"'{requirement_profile.name}' for ROS {args.ros_version}"
        )
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["preflight_error"] = str(exc)
        emit_status(f"functional requirement preflight failed: {exc}")
        write_json(results_dir / "result.json", result)
        write_markdown(results_dir / "summary.md", build_functional_summary(result))
        return 1

    try:
        _require_detected_camera(
            args.driver_setup,
            emit_status,
            ros_version=args.ros_version,
            ros_setup=args.ros_setup,
        )
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
            profile=profile,
            requirement_profile=requirement_profile,
            launch_file=launch_file,
            scenario=scenario,
            base_launch_args=base_launch_args,
            results_dir=results_dir,
            launch_log_path=launch_log_path,
            topic_log_path=topic_log_path,
            service_log_path=service_log_path,
            driver_setup=args.driver_setup,
            ros_version=args.ros_version,
            ros_setup=args.ros_setup,
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
        camera_name = str(launch_args.get("camera_name", "camera"))
        expanded_scenario = expand_launch_scenario(scenario, camera_name)
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
            ros_version=args.ros_version,
            ros_setup=args.ros_setup,
            status_callback=emit_status,
        )
        try:
            reboot_session.start()
            with RosHarness("orbbec_camera_functional_reboot_test", ros_version=args.ros_version) as harness:
                _wait_for_camera_ready(reboot_session, harness, camera_name, emit_status)
                tested_topic_names = {
                    item.get("name")
                    for item in scenario_result["topics"]
                    if item.get("status") == "passed"
                }
                image_topics = [
                    topic
                    for topic in expanded_scenario.topics
                    if topic.validator == "image" and topic.name in tested_topic_names
                ]
                topic_names = ", ".join(topic.name for topic in image_topics) or "<none>"
                emit_status(
                    f"calling reboot service and waiting for image streams: {topic_names}"
                )
                scenario_result["reboot"] = run_reboot_check(
                    harness,
                    reboot_spec,
                    image_topics,
                    camera_name,
                    service_log_path,
                    emit_status=emit_status,
                )
                scenario_result["reboot"]["required"] = (
                    reboot_spec.name
                    in set(
                        scenario_result.get("requirements", {}).get(
                            "required_services", []
                        )
                    )
                )
        except Exception as exc:  # noqa: BLE001
            scenario_result["reboot"] = {
                "name": reboot_spec.name,
                "type": reboot_spec.type,
                "required": (
                    reboot_spec.name
                    in set(
                        scenario_result.get("requirements", {}).get(
                            "required_services", []
                        )
                    )
                ),
                "status": "failed",
                "message": str(exc),
            }
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
    parser.add_argument("--launch-file", required=True, help="Driver launch file to test")
    parser.add_argument("--camera-name", default=None)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--usb-port", default=None)
    parser.add_argument("--config-file-path", default=None)
    parser.add_argument("--driver-setup", default=None)
    parser.add_argument(
        "--ros-version",
        choices=("1", "2"),
        default=os.environ.get("ORBBEC_ROS_VERSION", "2"),
        help="ROS major version to use",
    )
    parser.add_argument(
        "--ros-setup",
        default=os.environ.get("ORBBEC_ROS_SETUP", ""),
        help="ROS setup.bash/setup.zsh path",
    )
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
