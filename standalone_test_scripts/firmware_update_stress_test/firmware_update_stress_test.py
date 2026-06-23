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


ENV_READY_VAR = "FIRMWARE_UPDATE_STRESS_TEST_ENV_READY"
INTERRUPTED = False
SCRIPT_DIR = Path(__file__).resolve().parent
SUCCESS_RE = re.compile(
    r"Firmware tool completed successfully\. Updated (?P<updated>\d+)/(?P<total>\d+) target device\(s\)\."
)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handle_sigint(signum, frame) -> None:
    del signum, frame
    global INTERRUPTED
    INTERRUPTED = True
    raise KeyboardInterrupt


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
    def __call__(self, message: str) -> None:
        print(f"[{timestamp()}] {message}", flush=True)


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
                print(line, end="", flush=True)
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
        f"- ROS version: `{result.get('ros_version')}`",
        f"- Passed tests: `{result.get('passed_tests', 0)}`",
        f"- Total tests recorded: `{len(tests)}`",
        f"- Elapsed seconds: `{result.get('elapsed_seconds', 0.0):.1f}`",
        f"- Results dir: `{result.get('results_dir', '')}`",
        "",
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

    firmwares = normalize_firmware_paths(args.firmware)
    serial_numbers = split_csv_values(args.serial_number)
    selectors = [bool(serial_numbers), bool(args.usb_port), bool(args.device_ip)]
    if sum(1 for item in selectors if item) > 1:
        raise ValueError("only one selector can be used: --serial-number, --usb-port, or --device-ip")

    test_count = int(args.test_count)
    if test_count < 0:
        raise ValueError("--test-count must be >= 0")
    duration_seconds = parse_duration(args.duration, 300.0)
    restart_delay = float(args.restart_delay)
    if restart_delay < 0:
        raise ValueError("--restart-delay must be >= 0")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_firmware_update")
    results_dir = ensure_dir(
        Path(args.results_dir or (SCRIPT_DIR / "results" / run_id)).expanduser().resolve()
    )
    emit = StatusLogger()
    result: Dict[str, Any] = {
        "status": "passed",
        "ros_version": args.ros_version,
        "firmwares": [str(path) for path in firmwares],
        "serial_numbers": serial_numbers,
        "usb_port": args.usb_port,
        "device_ip": args.device_ip,
        "device_port": args.device_port,
        "test_count": test_count,
        "duration_seconds": duration_seconds,
        "restart_delay_seconds": restart_delay,
        "success_log_pattern": SUCCESS_RE.pattern,
        "results_dir": str(results_dir),
        "tests": [],
        "passed_tests": 0,
        "elapsed_seconds": 0.0,
    }

    emit(f"results dir: {results_dir}")
    emit(f"firmwares: {', '.join(path.name for path in firmwares)}")
    if serial_numbers:
        emit(f"target serial numbers: {','.join(serial_numbers)}")
    elif args.usb_port:
        emit(f"target usb port: {args.usb_port}")
    elif args.device_ip:
        emit(f"target device ip: {args.device_ip}")
    else:
        emit("target selector: default device")
    emit(f"test count: {test_count} ({'duration mode' if test_count == 0 else 'round mode'})")

    start_monotonic = time.monotonic()
    deadline = start_monotonic + duration_seconds
    test_index = 0

    try:
        while True:
            if test_count > 0 and test_index >= test_count:
                break
            if test_count == 0 and time.monotonic() >= deadline:
                break

            firmware_path = firmwares[test_index % len(firmwares)]
            test_index += 1
            test_name = f"test_{test_index:04d}"
            log_file = results_dir / "logs" / test_name / "update.log"
            command = build_update_command(
                ros_version=args.ros_version,
                firmware_path=firmware_path,
                serial_numbers=serial_numbers,
                usb_port=args.usb_port,
                device_ip=args.device_ip,
                device_port=args.device_port,
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

            emit(f"{test_name}: update firmware from {firmware_path}")
            returncode, output = run_command_to_log(command, runtime_env, results_dir, log_file)
            test_record["returncode"] = returncode
            success_log = parse_success_log(output)
            test_record["success_log"] = success_log

            if returncode != 0:
                raise RuntimeError(f"{test_name}: firmware_update_tool exited with {returncode}")
            if success_log is None:
                raise RuntimeError(f"{test_name}: success log was not found")
            if success_log["updated"] != success_log["total"]:
                raise RuntimeError(
                    f"{test_name}: success log updated {success_log['updated']}/"
                    f"{success_log['total']} targets"
                )
            if serial_numbers and success_log["total"] != len(serial_numbers):
                raise RuntimeError(
                    f"{test_name}: success log target count {success_log['total']} does not "
                    f"match serial count {len(serial_numbers)}"
                )

            test_record["status"] = "passed"
            test_record["message"] = (
                f"success log matched: updated {success_log['updated']}/{success_log['total']}"
            )
            test_record["ended_at"] = datetime.now().isoformat(timespec="seconds")
            result["passed_tests"] += 1
            emit(f"{test_name}: passed, updated {success_log['updated']}/{success_log['total']}")

            if restart_delay > 0 and (test_count == 0 or test_index < test_count):
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
        write_json(results_dir / "result.json", result)
        (results_dir / "summary.md").write_text(build_summary(result), encoding="utf-8")
        signal.signal(signal.SIGINT, previous_sigint_handler)

    if result["status"] == "passed":
        emit(f"test finished successfully, passed_tests={result['passed_tests']}")
        return 0
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args():
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
            "--firmware /path/fw_a.bin --firmware /path/fw_b.bin --test-count 10\n\n"
            "  python3 ./firmware_update_stress_test/firmware_update_stress_test.py "
            "--serial-number SN1,SN2 --firmware /path/fw.bin --test-count 3\n"
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
        "--serial-number",
        action="append",
        default=[],
        help="Target serial number(s). Repeat or pass comma-separated values for batch update.",
    )
    parser.add_argument("--usb-port", default="", help="Single target USB port selector")
    parser.add_argument("--device-ip", default="", help="Single target network device IP selector")
    parser.add_argument("--device-port", default="8090", help="Network device port")
    parser.add_argument("--test-count", type=int, default=10, help="Update command invocations; 0 means duration mode")
    parser.add_argument("--duration", default="300", help="Duration used when --test-count is 0; supports 300, 15m, 2h")
    parser.add_argument("--restart-delay", default="2", help="Delay seconds between update commands")
    parser.add_argument("--reconnect-timeout-sec", default="120", help="Passed to firmware_update_tool")
    parser.add_argument("--reconnect-poll-ms", default="1000", help="Passed to firmware_update_tool")
    parser.add_argument("--sdk-log-level", default="off", help="Passed to firmware_update_tool")
    parser.add_argument("--continue-on-error", action="store_true", help="Passed to firmware_update_tool")
    parser.add_argument("--results-dir", default="")
    return parser.parse_args()


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
