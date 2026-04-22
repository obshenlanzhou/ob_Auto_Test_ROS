from __future__ import annotations

import csv
import json
import os
import shlex
import signal
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_ROS_SETUP = "/opt/ros/humble/setup.bash"
DEFAULT_CAMERA_SETUP_DIR = Path("/home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install")


def _first_existing_setup(base_dir: Path) -> str:
    for name in ("setup.bash", "setup.zsh"):
        candidate = base_dir / name
        if candidate.is_file():
            return str(candidate)
    return str(base_dir / "setup.bash")


DEFAULT_CAMERA_SETUP = _first_existing_setup(DEFAULT_CAMERA_SETUP_DIR)


def _find_auto_test_ws() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "run_camera_auto_test.sh").is_file() and (parent / "src").is_dir():
            return parent

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "run_camera_auto_test.sh").is_file() and (parent / "src").is_dir():
            return parent

    return cwd


AUTO_TEST_WS = _find_auto_test_ws()
CORE_PACKAGE_ROOT = AUTO_TEST_WS / "src" / "orbbec_camera_auto_test"
RESULTS_ROOT = AUTO_TEST_WS / "results"
UI_RESULTS_ROOT = RESULTS_ROOT / "ui_runs"
CONFIG_PATH = RESULTS_ROOT / "ui_config.json"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_latest_csv_row(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as stream:
            header = stream.readline().decode("utf-8", errors="replace").strip()
            if not header:
                return {}
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            chunk_size = min(8192, end)
            stream.seek(max(0, end - chunk_size))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return {}
    if lines[0] == header and len(lines) == 1:
        return {}
    last_line = lines[-1]
    try:
        headers = next(csv.reader([header]))
        values = next(csv.reader([last_line]))
    except csv.Error:
        return {}
    return dict(zip(headers, values))


def _latest_file(root: Path, name: str) -> Path | None:
    candidates = [path for path in root.rglob(name) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _float_value(payload: Dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(payload.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _int_value(payload: Dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(payload.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _format_topic_label(label: str) -> str:
    if not label:
        return ""
    topic = "/" + label.replace("_", "/")
    return (
        topic.replace("/image/raw", "/image_raw")
        .replace("/left/ir", "/left_ir")
        .replace("/right/ir", "/right_ir")
    )


def build_performance_metrics(run_root: Path) -> Dict[str, Any]:
    system_path = _latest_file(run_root, "system_usage.csv")
    fps_path = _latest_file(run_root, "fps.csv")
    system_row = _read_latest_csv_row(system_path) if system_path else {}
    fps_row = _read_latest_csv_row(fps_path) if fps_path else {}

    elapsed_seconds = max(
        _float_value(system_row, "elapsed_seconds"),
        _float_value(fps_row, "elapsed_seconds"),
    )
    topics: List[Dict[str, Any]] = []
    suffixes = (
        "_ideal_fps",
        "_current_fps",
        "_avg_fps",
        "_dropped_frames",
        "_drop_rate",
    )
    labels = sorted(
        {
            key[: -len(suffix)]
            for key in fps_row
            for suffix in suffixes
            if key.endswith(suffix)
        }
    )
    for label in labels:
        topics.append(
            {
                "label": label,
                "topic": _format_topic_label(label),
                "ideal_fps": _float_value(fps_row, f"{label}_ideal_fps"),
                "current_fps": _float_value(fps_row, f"{label}_current_fps"),
                "avg_fps": _float_value(fps_row, f"{label}_avg_fps"),
                "dropped_frames": _int_value(fps_row, f"{label}_dropped_frames"),
                "drop_rate": _float_value(fps_row, f"{label}_drop_rate"),
            }
        )

    return {
        "available": bool(system_row or fps_row),
        "elapsed_seconds": elapsed_seconds,
        "pid_count": _int_value(system_row, "pid_count"),
        "cpu_percent": _float_value(system_row, "cpu_percent"),
        "memory_rss_mb": _float_value(system_row, "memory_rss_mb"),
        "fps_topics": topics,
        "system_csv": str(system_path) if system_path else "",
        "fps_csv": str(fps_path) if fps_path else "",
    }


def load_config() -> Dict[str, Any]:
    config = read_json(CONFIG_PATH, {})
    return {
        "ros_setup": config.get("ros_setup") or DEFAULT_ROS_SETUP,
        "camera_setup": config.get("camera_setup") or DEFAULT_CAMERA_SETUP,
        "host": config.get("host") or "127.0.0.1",
        "port": int(config.get("port") or 8000),
    }


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config()
    for key in ("ros_setup", "camera_setup", "host", "port"):
        if key in payload:
            config[key] = payload[key]
    write_json(CONFIG_PATH, config)
    return config


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _append_arg(args: List[str], name: str, value: Any) -> None:
    text = _safe_text(value)
    if text:
        args.extend([name, text])


def _parse_extra_launch_args(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [line.strip() for line in str(raw or "").splitlines() if line.strip()]


def _build_runner_args(payload: Dict[str, Any], mode: str, results_dir: Path) -> List[str]:
    module = (
        "orbbec_camera_auto_test.functional_runner"
        if mode == "functional"
        else "orbbec_camera_auto_test.performance_runner"
    )
    args = ["python3", "-m", module]
    _append_arg(args, "--profile", payload.get("profile") or "gemini_330_series")
    _append_arg(args, "--results-dir", str(results_dir))
    _append_arg(args, "--launch-file", payload.get("launch_file"))
    _append_arg(args, "--camera-name", payload.get("camera_name"))
    _append_arg(args, "--serial-number", payload.get("serial_number"))
    _append_arg(args, "--usb-port", payload.get("usb_port"))
    _append_arg(args, "--config-file-path", payload.get("config_file_path"))

    if mode == "performance":
        _append_arg(args, "--performance-scenario", payload.get("performance_scenario"))
        _append_arg(args, "--duration", payload.get("duration"))

    for launch_arg in _parse_extra_launch_args(payload.get("launch_args")):
        args.extend(["--launch-arg", launch_arg])
    return args


def _quote_command(args: List[str]) -> str:
    return " ".join(shlex.quote(item) for item in args)


def _matching_setup_variant(path: str, suffix: str) -> str:
    setup_path = Path(path)
    if setup_path.name not in {"setup.bash", "setup.zsh"}:
        return path
    candidate = setup_path.with_name(f"setup.{suffix}")
    return str(candidate) if candidate.is_file() else path


def _shell_for_setup(camera_setup: str) -> str:
    return "zsh" if camera_setup.endswith(".zsh") else "bash"


def _build_shell_script(payload: Dict[str, Any], run_root: Path) -> tuple[str, List[str], str]:
    mode = _safe_text(payload.get("mode")) or "functional"
    camera_setup = _safe_text(payload.get("camera_setup"))
    shell = _shell_for_setup(camera_setup)
    ros_setup = _safe_text(payload.get("ros_setup")) or DEFAULT_ROS_SETUP
    if shell == "zsh":
        ros_setup = _matching_setup_variant(ros_setup, "zsh")
    commands = [
        "set -e",
        f"source {shlex.quote(ros_setup)}",
    ]
    if camera_setup:
        commands.append(f"source {shlex.quote(camera_setup)}")
    commands.append(f"export PYTHONPATH={shlex.quote(str(CORE_PACKAGE_ROOT))}:\"${{PYTHONPATH:-}}\"")
    commands.append(f"cd {shlex.quote(str(AUTO_TEST_WS))}")

    displayed: List[str] = []
    if mode in ("functional", "all"):
        functional_dir = run_root / "functional" if mode == "all" else run_root
        args = _build_runner_args(payload, "functional", functional_dir)
        displayed.append(_quote_command(args))
        commands.append(_quote_command(args))
    if mode in ("performance", "all"):
        performance_dir = run_root / "performance" if mode == "all" else run_root
        args = _build_runner_args(payload, "performance", performance_dir)
        displayed.append(_quote_command(args))
        commands.append(_quote_command(args))

    return "\n".join(commands), displayed, shell


def validate_run_payload(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    mode = _safe_text(payload.get("mode")) or "functional"
    if mode not in {"functional", "performance", "all"}:
        errors.append(f"unsupported mode: {mode}")

    ros_setup = Path(_safe_text(payload.get("ros_setup")) or DEFAULT_ROS_SETUP)
    if not ros_setup.is_file():
        errors.append(f"ROS setup file not found: {ros_setup}")

    camera_setup = _safe_text(payload.get("camera_setup"))
    if camera_setup and not Path(camera_setup).is_file():
        errors.append(f"camera ROS setup file not found: {camera_setup}")
    if camera_setup.endswith(".zsh") and shutil.which("zsh") is None:
        errors.append("zsh setup was selected, but zsh was not found in PATH")

    for launch_arg in _parse_extra_launch_args(payload.get("launch_args")):
        if "=" not in launch_arg:
            errors.append(f"launch arg must be KEY=VALUE: {launch_arg}")
    return errors


@dataclass
class TestJob:
    run_id: str
    mode: str
    run_root: Path
    command_lines: List[str]
    shell: str
    process: Optional[subprocess.Popen[str]] = None
    status: str = "starting"
    exit_code: Optional[int] = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    ended_at: Optional[str] = None
    logs: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_log(self, line: str) -> None:
        text = line.rstrip("\n")
        with self.lock:
            self.logs.append(text)
            if len(self.logs) > 2000:
                self.logs = self.logs[-2000:]
        log_path = self.run_root / "ui_stdout.log"
        ensure_dir(log_path.parent)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    def snapshot(self, log_offset: int = 0) -> Dict[str, Any]:
        with self.lock:
            logs = list(self.logs)
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "results_dir": str(self.run_root),
            "command_lines": self.command_lines,
            "shell": self.shell,
            "performance": build_performance_metrics(self.run_root),
            "log_offset": len(logs),
            "logs": logs[log_offset:],
        }


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[TestJob] = None

    def current_snapshot(self, log_offset: int = 0) -> Dict[str, Any]:
        with self._lock:
            job = self._current
        if job is None:
            return {"status": "idle", "logs": [], "log_offset": 0}
        return job.snapshot(log_offset=log_offset)

    def start(self, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            if self._current is not None and self._current.status in {"starting", "running", "stopping"}:
                return 409, {"error": "a test is already running"}

        errors = validate_run_payload(payload)
        if errors:
            return 400, {"errors": errors}

        config = save_config(
            {
                "ros_setup": payload.get("ros_setup") or DEFAULT_ROS_SETUP,
                "camera_setup": payload.get("camera_setup") or DEFAULT_CAMERA_SETUP,
            }
        )
        payload = {**payload, **config}
        mode = _safe_text(payload.get("mode")) or "functional"
        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}"
        run_root = ensure_dir(UI_RESULTS_ROOT / run_id)
        script, command_lines, shell = _build_shell_script(payload, run_root)
        write_json(
            run_root / "ui_request.json",
            {
                "run_id": run_id,
                "mode": mode,
                "request": payload,
                "command_lines": command_lines,
                "auto_test_ws": str(AUTO_TEST_WS),
            },
        )

        job = TestJob(
            run_id=run_id,
            mode=mode,
            run_root=run_root,
            command_lines=command_lines,
            shell=shell,
        )
        with self._lock:
            self._current = job

        thread = threading.Thread(target=self._run_job, args=(job, script), daemon=True)
        thread.start()
        return 200, job.snapshot()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            job = self._current
        if job is None or job.process is None or job.status not in {"starting", "running"}:
            return {"status": "idle"}

        job.status = "stopping"
        job.add_log("[UI] stopping test with SIGINT")
        try:
            os.killpg(os.getpgid(job.process.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        return job.snapshot()

    def _run_job(self, job: TestJob, script: str) -> None:
        job.status = "running"
        job.add_log(f"[UI] run id: {job.run_id}")
        job.add_log(f"[UI] results dir: {job.run_root}")
        job.add_log(f"[UI] shell: {job.shell}")
        for line in job.command_lines:
            job.add_log(f"[UI] command: {line}")

        try:
            job.process = subprocess.Popen(
                [job.shell, "-lc", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            assert job.process.stdout is not None
            for line in job.process.stdout:
                job.add_log(line)
            job.exit_code = job.process.wait()
            if job.status == "stopping" or job.exit_code == 130:
                job.status = "interrupted"
            elif job.exit_code == 0:
                job.status = "passed"
            else:
                job.status = "failed"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.exit_code = 1
            job.add_log(f"[UI] failed to run test: {exc}")
        finally:
            job.ended_at = datetime.now().isoformat(timespec="seconds")
            write_json(
                job.run_root / "ui_status.json",
                {
                    "run_id": job.run_id,
                    "mode": job.mode,
                    "status": job.status,
                    "exit_code": job.exit_code,
                    "started_at": job.started_at,
                    "ended_at": job.ended_at,
                    "results_dir": str(job.run_root),
                    "command_lines": job.command_lines,
                    "shell": job.shell,
                },
            )
            job.add_log(f"[UI] finished with status={job.status}, exit_code={job.exit_code}")
