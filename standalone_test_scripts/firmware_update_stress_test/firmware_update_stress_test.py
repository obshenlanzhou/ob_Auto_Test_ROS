#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from _test_protocol import (
    EventWriter,
    artifact_list,
    atomic_write_json,
    collect_test_environment,
    contract_result,
    install_terminal_log,
    iso_now,
    namespace_request,
    parse_camera,
    test_environment_markdown,
)

ENV_READY_VAR = "FIRMWARE_UPDATE_STRESS_TEST_ENV_READY"
INTERRUPTED = False
SCRIPT_DIR = Path(__file__).resolve().parent
TOOL_VERSION = "2.0.0"
TEST_ID = "firmware_update_stress_test"
SUCCESS_RE = re.compile(
    r"Firmware tool completed successfully\. Updated (?P<updated>\d+)/(?P<total>\d+) target device\(s\)\."
)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handle_sigint(signum, frame) -> None:
    del signum, frame
    global INTERRUPTED
    INTERRUPTED = True


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_duration(value: Any, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    raw = str(value).strip().lower()
    multiplier = 1.0
    if raw.endswith("s"):
        raw = raw[:-1]
    elif raw.endswith("m"):
        raw = raw[:-1]
        multiplier = 60.0
    elif raw.endswith("h"):
        raw = raw[:-1]
        multiplier = 3600.0
    duration = float(raw) * multiplier
    if duration <= 0.0:
        raise ValueError("duration values must be > 0")
    return duration


def split_csv_values(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                result.append(item)
    return result


def normalize_firmware_paths(values: List[str]) -> List[Path]:
    paths = [Path(value).expanduser().resolve() for value in values if str(value).strip()]
    if not paths:
        raise ValueError("at least one --firmware is required")
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"firmware file not found: {path}")
    return paths


class StatusLogger:
    def __init__(self, events: Optional[EventWriter] = None) -> None:
        self.events = events

    def __call__(self, message: str, *, event: str = "log", **fields: Any) -> None:
        print(f"[{timestamp()}] {message}", flush=True)
        if self.events is not None:
            self.events.emit(event, message, **fields)


def capture_sourced_env(ros_setup: str, driver_setup: str, ros_version: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["ROS_VERSION"] = ros_version
    command_parts = []
    for setup_file in (ros_setup, driver_setup):
        setup_file = str(setup_file or "").strip()
        if not setup_file:
            continue
        setup_path = Path(setup_file).expanduser()
        if not setup_path.is_file():
            raise FileNotFoundError(f"setup file not found: {setup_path}")
        command_parts.append(f"source {shlex.quote(str(setup_path))} >/dev/null 2>&1")
    if not command_parts:
        return env
    command = " && ".join(command_parts) + " && env -0"
    raw_output = subprocess.check_output(["bash", "-lc", command], env=env)
    sourced_env: Dict[str, str] = {}
    for chunk in raw_output.split(b"\0"):
        if not chunk or b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        sourced_env[key.decode("utf-8")] = value.decode("utf-8")
    sourced_env["ROS_VERSION"] = ros_version
    sourced_env["PYTHONUNBUFFERED"] = "1"
    return sourced_env


def prepare_runtime_env(args) -> Dict[str, str]:
    if os.environ.get(ENV_READY_VAR) == "1":
        runtime_env = dict(os.environ)
        runtime_env["ROS_VERSION"] = args.ros_version
        runtime_env["PYTHONUNBUFFERED"] = "1"
        return runtime_env

    runtime_env = capture_sourced_env(args.ros_setup, args.driver_setup, args.ros_version)
    runtime_env[ENV_READY_VAR] = "1"
    if args.ros_setup or args.driver_setup:
        executable = sys.executable or "python3"
        os.execvpe(executable, [executable, *sys.argv], runtime_env)
    return runtime_env


def build_update_command(
    *,
    ros_version: str,
    firmware_path: Path,
    serial_numbers: List[str],
    usb_port: str,
    device_ip: str,
    device_port: str,
    reconnect_timeout_sec: str,
    reconnect_poll_ms: str,
    sdk_log_level: str,
    continue_on_error: bool,
) -> List[str]:
    command = (
        ["rosrun", "orbbec_camera", "firmware_update_tool"]
        if ros_version == "1"
        else ["ros2", "run", "orbbec_camera", "firmware_update_tool", "--"]
    )
    if serial_numbers:
        command.extend(["--serial_number", ",".join(serial_numbers)])
    if usb_port:
        command.extend(["--usb_port", usb_port])
    if device_ip:
        command.extend(["--device_ip", device_ip])
    if device_port:
        command.extend(["--device_port", device_port])
    command.extend(["--firmware_path", str(firmware_path)])
    if reconnect_timeout_sec:
        command.extend(["--reconnect_timeout_sec", reconnect_timeout_sec])
    if reconnect_poll_ms:
        command.extend(["--reconnect_poll_ms", reconnect_poll_ms])
    if sdk_log_level:
        command.extend(["--sdk_log_level", sdk_log_level])
    if continue_on_error:
        command.append("--continue_on_error")
    return command


def run_command_to_log(
    command: List[str],
    env: Dict[str, str],
    work_dir: Path,
    log_file: Path,
) -> tuple[int, str]:
    ensure_dir(log_file.parent)
    output_chunks: List[str] = []
    process: Optional[subprocess.Popen[str]] = None
    with log_file.open("w", encoding="utf-8", errors="replace") as handle:
        handle.write(f"# command: {shlex.join(command)}\n")
        handle.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(work_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            assert process.stdout is not None
            for line in process.stdout:
                output_chunks.append(line)
                handle.write(line)
                handle.flush()
            return process.wait(), "".join(output_chunks)
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                if hasattr(os, "killpg"):
                    os.killpg(process.pid, signal.SIGINT)
                else:
                    process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise


def parse_success_log(output: str) -> Optional[Dict[str, int]]:
    match = None
    for match in SUCCESS_RE.finditer(output):
        pass
    if match is None:
        return None
    return {
        "updated": int(match.group("updated")),
        "total": int(match.group("total")),
    }


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_summary(result: Dict[str, Any]) -> str:
    tests = result.get("tests", [])
    failed_tests = [test for test in tests if test.get("status") != "passed"]
    status_counts: Dict[str, int] = {}
    for test in tests:
        status = str(test.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    lines = [
        "# Firmware Update Stress Test Summary",
        "",
        f"- Status: `{result.get('status')}`",
        f"- Tool version: `{result.get('tool_version', '')}`",
        f"- ROS version: `{result.get('ros_version')}`",
        f"- Passed tests: `{result.get('passed_tests', 0)}`",
        f"- Total tests recorded: `{len(tests)}`",
        f"- Elapsed seconds: `{result.get('elapsed_seconds', 0.0):.1f}`",
        f"- Results dir: `{result.get('results_dir', '')}`",
        "",
        *test_environment_markdown(result.get("environment", {})),
        "## Targets",
        "",
    ]
    serial_numbers = result.get("serial_numbers") or []
    if serial_numbers:
        lines.append(f"- Serial numbers: `{','.join(serial_numbers)}`")
    elif result.get("usb_port"):
        lines.append(f"- USB port: `{result.get('usb_port')}`")
    elif result.get("device_ip"):
        lines.append(f"- Device IP: `{result.get('device_ip')}`")
    else:
        lines.append("- Selector: default device")

    lines.extend(["", "## Firmware List", ""])
    for path in result.get("firmwares", []):
        lines.append(f"- `{path}`")

    if result.get("error"):
        lines.extend(["", "## Error", "", str(result["error"])])

    lines.extend(["", "## Test Statistics", ""])
    if status_counts:
        for status in sorted(status_counts):
            lines.append(f"- {status}: {status_counts[status]}")
    else:
        lines.append("- No tests recorded")

    lines.extend(["", "## Failures", ""])
    if not failed_tests:
        lines.append("- None")
    else:
        for test in failed_tests:
            test_index = int(test.get("test_index") or 0)
            firmware_name = Path(str(test.get("firmware_path", ""))).name
            lines.append(
                f"- test_{test_index:04d}: {test.get('status')} ({firmware_name}), "
                f"returncode={test.get('returncode')}"
            )
            if test.get("message"):
                lines.append(f"  {test['message']}")
            if test.get("log"):
                lines.append(f"  - log: {test['log']}")
    return "\n".join(lines) + "\n"


def run(args) -> int:
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    runtime_env = prepare_runtime_env(args)
    environment = collect_test_environment(args)

    firmwares = normalize_firmware_paths(args.firmware)
    cameras = [parse_camera(raw) for raw in args.camera] or [parse_camera("name=camera")]
    serial_numbers = [item["serial_number"] for item in cameras if item["serial_number"]]
    usb_ports = {item["usb_port"] for item in cameras if item["usb_port"]}
    device_ips = {item["device_ip"] for item in cameras if item["device_ip"]}
    device_ports = {item["device_port"] for item in cameras if item["device_port"]}
    if len(usb_ports) > 1:
        raise ValueError("firmware update accepts at most one distinct usb-port")
    if len(device_ips) > 1:
        raise ValueError("firmware update accepts at most one distinct device-ip")
    if len(device_ports) > 1:
        raise ValueError("firmware update accepts at most one distinct device-port")
    usb_port = next(iter(usb_ports), "")
    device_ip = next(iter(device_ips), "")
    device_port = next(iter(device_ports), "8090")

    run_count = args.run_count
    duration_text = str(args.duration or "").strip()
    if not duration_text and run_count is None:
        raise ValueError("at least one of --duration or --run-count is required")
    if run_count is not None and run_count <= 0:
        raise ValueError("--run-count must be > 0")
    duration_seconds = (
        parse_duration(duration_text, 0.0) if duration_text else None
    )
    restart_delay = float(args.restart_delay)
    if restart_delay < 0:
        raise ValueError("--restart-delay must be >= 0")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_firmware_update") + f"_v{TOOL_VERSION}"
    run_started_at = iso_now()
    results_dir = ensure_dir(
        Path(args.results_dir or (SCRIPT_DIR / "results" / run_id)).expanduser().resolve()
    )
    install_terminal_log(results_dir / "terminal.log")
    events = EventWriter(results_dir / "events.jsonl")
    emit = StatusLogger(events)
    result: Dict[str, Any] = {
        "status": "passed",
        "tool_version": TOOL_VERSION,
        "environment": environment,
        "ros_version": args.ros_version,
        "firmwares": [str(path) for path in firmwares],
        "cameras": cameras,
        "run_count": run_count,
        "continue_on_failure": args.continue_on_failure,
        "duration_limit_seconds": duration_seconds,
        "restart_delay_seconds": restart_delay,
        "success_log_pattern": SUCCESS_RE.pattern,
        "results_dir": str(results_dir),
        "tests": [],
        "passed_tests": 0,
        "elapsed_seconds": 0.0,
    }

    emit(f"tool version: {TOOL_VERSION}")
    emit(f"results dir: {results_dir}")
    emit(f"firmwares: {', '.join(path.name for path in firmwares)}")
    emit("test started", event="phase", phase="starting")
    if serial_numbers:
        emit(f"target serial numbers: {','.join(serial_numbers)}")
    elif usb_port:
        emit(f"target usb port: {usb_port}")
    elif device_ip:
        emit(f"target device ip: {device_ip}")
    else:
        emit("target selector: default device")
    emit(f"run count: {run_count if run_count is not None else 'duration-limited'}")

    start_monotonic = time.monotonic()
    deadline = (
        start_monotonic + duration_seconds
        if duration_seconds is not None
        else None
    )
    test_index = 0

    try:
        while True:
            if INTERRUPTED:
                result["status"] = "interrupted"
                emit(
                    "stop requested; current firmware update completed",
                    event="phase",
                    phase="stopped-at-safe-point",
                )
                break
            if run_count is not None and test_index >= run_count:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break

            firmware_path = firmwares[test_index % len(firmwares)]
            test_index += 1
            test_name = f"test_{test_index:04d}"
            progress_label = f"{test_index}/{run_count if run_count is not None else 'duration'}"
            log_file = results_dir / "logs" / test_name / "update.log"
            command = build_update_command(
                ros_version=args.ros_version,
                firmware_path=firmware_path,
                serial_numbers=serial_numbers,
                usb_port=usb_port,
                device_ip=device_ip,
                device_port=device_port,
                reconnect_timeout_sec=args.reconnect_timeout_sec,
                reconnect_poll_ms=args.reconnect_poll_ms,
                sdk_log_level=args.sdk_log_level,
                continue_on_error=args.continue_on_error,
            )
            test_record: Dict[str, Any] = {
                "test_index": test_index,
                "firmware_path": str(firmware_path),
                "command": command,
                "log": str(log_file),
                "status": "running",
                "message": "",
                "returncode": None,
                "success_log": None,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ended_at": "",
            }
            result["tests"].append(test_record)

            emit(
                f"{test_name} ({progress_label}): update firmware from {firmware_path.name}",
                event="progress",
                current=test_index,
                total=run_count,
                phase="updating",
            )
            test_env = dict(runtime_env)
            test_env["ORBBEC_LOG_DIR"] = str(ensure_dir(log_file.parent / "sdk"))
            command_error = ""
            try:
                returncode, output = run_command_to_log(
                    command, test_env, results_dir, log_file
                )
            except Exception as exc:  # noqa: BLE001
                returncode, output = None, ""
                command_error = f"{test_name}: update command failed: {exc}"
            test_record["returncode"] = returncode
            success_log = parse_success_log(output)
            test_record["success_log"] = success_log

            failure_message = command_error
            if not failure_message and returncode != 0:
                failure_message = (
                    f"{test_name}: firmware_update_tool exited with {returncode}"
                )
            elif not failure_message and success_log is None:
                failure_message = f"{test_name}: success log was not found"
            elif not failure_message and success_log["updated"] != success_log["total"]:
                failure_message = (
                    f"{test_name}: success log updated {success_log['updated']}/"
                    f"{success_log['total']} targets"
                )
            elif (
                not failure_message
                and serial_numbers
                and success_log["total"] != len(serial_numbers)
            ):
                failure_message = (
                    f"{test_name}: success log target count {success_log['total']} does not "
                    f"match serial count {len(serial_numbers)}"
                )

            if failure_message:
                test_record["status"] = "failed"
                test_record["message"] = failure_message
                test_record["ended_at"] = datetime.now().isoformat(timespec="seconds")
                result["status"] = "failed"
                result.setdefault("errors", []).append(failure_message)
                emit(failure_message)
                if not args.continue_on_failure:
                    emit(
                        "stopping after failed update "
                        "(use --continue-on-failure to continue)"
                    )
                    break
                emit(f"{test_name}: continuing after failure")
                if restart_delay > 0 and (
                    run_count is None or test_index < run_count
                ):
                    time.sleep(restart_delay)
                continue

            test_record["status"] = "passed"
            test_record["message"] = (
                f"success log matched: updated {success_log['updated']}/{success_log['total']}"
            )
            test_record["ended_at"] = datetime.now().isoformat(timespec="seconds")
            result["passed_tests"] += 1
            emit(
                f"{test_name} ({progress_label}): passed, "
                f"updated {success_log['updated']}/{success_log['total']}",
                event="progress",
                current=test_index,
                total=run_count,
                phase="completed-cycle",
            )

            if INTERRUPTED:
                result["status"] = "interrupted"
                emit(
                    "stop requested; current firmware update completed",
                    event="phase",
                    phase="stopped-at-safe-point",
                )
                break
            if restart_delay > 0 and (
                run_count is None or test_index < run_count
            ):
                time.sleep(restart_delay)
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        emit("test interrupted by user")
        if result["tests"] and result["tests"][-1].get("status") == "running":
            result["tests"][-1]["status"] = "interrupted"
            result["tests"][-1]["message"] = "interrupted by user"
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            result["status"] = "interrupted"
            emit("test interrupted by user")
            if result["tests"] and result["tests"][-1].get("status") == "running":
                result["tests"][-1]["status"] = "interrupted"
                result["tests"][-1]["message"] = "interrupted by user"
        else:
            result["status"] = "failed"
            result["error"] = str(exc)
            emit(f"test failed: {exc}")
            if result["tests"] and result["tests"][-1].get("status") == "running":
                result["tests"][-1]["status"] = "failed"
                result["tests"][-1]["message"] = str(exc)
    finally:
        result["elapsed_seconds"] = time.monotonic() - start_monotonic
        for test in result.get("tests", []):
            if not test.get("ended_at"):
                test["ended_at"] = datetime.now().isoformat(timespec="seconds")
        (results_dir / "summary.md").write_text(build_summary(result), encoding="utf-8")
        emit(
            f"test finished with status {result['status']}",
            event="completed",
            status=result["status"],
        )
        payload = contract_result(
            test_id=TEST_ID,
            run_id=run_id,
            started_at=run_started_at,
            ended_at=iso_now(),
            request=namespace_request(args),
            details=result,
            summary={
                "passed_runs": result["passed_tests"],
                "completed_runs": len(result["tests"]),
            },
            artifacts=artifact_list(results_dir),
        )
        atomic_write_json(results_dir / "result.json", payload)
        signal.signal(signal.SIGINT, previous_sigint_handler)

    if result["status"] == "passed":
        emit(f"test finished successfully, passed_tests={result['passed_tests']}")
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Repeatedly call orbbec_camera firmware_update_tool with a firmware list and "
            "verify the tool success log."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 ./firmware_update_stress_test/firmware_update_stress_test.py "
            "--ros-version 2 --driver-setup /path/to/install/setup.bash "
            "--firmware /path/fw_a.bin --firmware /path/fw_b.bin --run-count 10\n\n"
            "  python3 ./firmware_update_stress_test/firmware_update_stress_test.py "
            "--camera name=camera,serial-number=SN1 "
            "--firmware /path/fw.bin --run-count 3\n"
        ),
    )
    parser.add_argument("--ros-version", choices=("1", "2"), default=os.environ.get("ROS_VERSION", "2"))
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument("--driver-setup", default=os.environ.get("ORBBEC_CAMERA_SETUP", ""))
    parser.add_argument(
        "--firmware",
        action="append",
        default=[],
        help="Firmware image path. Repeat to cycle through multiple files in order.",
    )
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        help=(
            "Camera target as comma-separated KEY=VALUE fields. Supported keys: "
            "name, serial-number, usb-port, device-ip, device-port, config-file-path."
        ),
    )
    parser.add_argument(
        "--run-count", type=int, default=None, help="Optional maximum update cycles"
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue with the next update cycle after a failed cycle (default: stop)",
    )
    parser.add_argument(
        "--duration",
        default="",
        help="Optional maximum wall time, such as 300, 15m, or 2h",
    )
    parser.add_argument("--restart-delay", default="2", help="Delay seconds between update commands")
    parser.add_argument("--reconnect-timeout-sec", default="120", help="Passed to firmware_update_tool")
    parser.add_argument("--reconnect-poll-ms", default="1000", help="Passed to firmware_update_tool")
    parser.add_argument(
        "--sdk-log-level",
        choices=("debug", "info", "warn", "error", "fatal", "off", "none"),
        default="debug",
        help="Passed to firmware_update_tool",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Passed to firmware_update_tool; does not control stress-test cycle continuation",
    )
    parser.add_argument("--results-dir", default="")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(TOOL_VERSION),
    )
    args = parser.parse_args(argv)
    if not str(args.duration or "").strip() and args.run_count is None:
        parser.error("at least one of --duration or --run-count is required")
    return args


def main() -> None:
    try:
        sys.exit(run(parse_args()))
    except KeyboardInterrupt:
        print(f"[{timestamp()}] test interrupted by user", flush=True)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        if INTERRUPTED:
            print(f"[{timestamp()}] test interrupted by user", flush=True)
            sys.exit(130)
        print(f"[{timestamp()}] error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
