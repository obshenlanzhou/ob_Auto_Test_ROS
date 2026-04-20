from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

import psutil

from .reporter import ensure_dir


ROS1_VARS = ("ROS_MASTER_URI", "ROS_ROOT", "ROS_PACKAGE_PATH", "ROS_DISTRO", "ROS_ETC_DIR")
PATH_LIKE_VARS = ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "AMENT_PREFIX_PATH", "CMAKE_PREFIX_PATH")
CAMERA_PROCESS_HINTS = (
    "component_container",
    "component_container_mt",
    "orbbec_camera_node",
    "nodelet",
)


def _parse_env_output(raw_output: bytes) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for chunk in raw_output.split(b"\0"):
        if not chunk or b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        env[key.decode("utf-8")] = value.decode("utf-8")
    return env


def sanitize_ros_env(source_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(source_env or os.environ)
    for var_name in ROS1_VARS:
        env.pop(var_name, None)

    for var_name in PATH_LIKE_VARS:
        value = env.get(var_name)
        if not value:
            continue
        env[var_name] = os.pathsep.join(
            part for part in value.split(os.pathsep) if "/opt/ros/one" not in part
        )
    env["ROS_VERSION"] = "2"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def capture_runtime_env(driver_setup: Optional[str] = None) -> Dict[str, str]:
    if driver_setup and not Path(driver_setup).is_file():
        raise FileNotFoundError(f"Driver setup file not found: {driver_setup}")

    command_parts = ["source /opt/ros/humble/setup.bash >/dev/null 2>&1"]
    if driver_setup:
        command_parts.append(f"source {shlex.quote(driver_setup)} >/dev/null 2>&1")
    command = " && ".join(command_parts) + " && env -0"
    raw = subprocess.check_output(
        ["bash", "-lc", command],
        env=sanitize_ros_env(),
    )
    return sanitize_ros_env(_parse_env_output(raw))


def launch_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def discover_orbbec_devices(driver_setup: Optional[str] = None) -> Dict[str, Any]:
    runtime_env = capture_runtime_env(driver_setup)
    command = ["ros2", "run", "orbbec_camera", "list_devices_node"]
    try:
        result = subprocess.run(
            command,
            env=runtime_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20.0,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "device_count": 0,
            "message": f"device discovery command failed: {exc}",
            "output": "",
        }

    output = (result.stdout or "").strip()
    device_count = output.count("- Name:")
    success = result.returncode == 0
    if device_count > 0:
        message = f"found {device_count} Orbbec device(s)"
    elif success:
        message = "no Orbbec device detected"
    else:
        message = "device discovery command returned an error"

    return {
        "success": success,
        "device_count": device_count,
        "message": message,
        "output": output,
        "returncode": result.returncode,
    }


class TestSession:
    def __init__(
        self,
        launch_file: str,
        launch_args: Dict[str, Any],
        work_dir: Path,
        log_path: Path,
        driver_setup: Optional[str] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.launch_file = launch_file
        self.launch_args = dict(launch_args)
        self.work_dir = ensure_dir(work_dir)
        self.log_path = log_path
        self.driver_setup = driver_setup
        self.status_callback = status_callback
        self.process: Optional[subprocess.Popen[str]] = None
        self.root_pid: Optional[int] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._log_stream = None
        self._captured_lines = []
        self._primed_pids: Set[int] = set()
        self._process_cache: Dict[int, psutil.Process] = {}
        self.process_group_id: Optional[int] = None

    def command(self) -> list[str]:
        command = ["ros2", "launch", "orbbec_camera", self.launch_file]
        for key, value in sorted(self.launch_args.items()):
            if value is None or value == "":
                continue
            command.append(f"{key}:={launch_value(value)}")
        return command

    def _emit_status(self, message: str) -> None:
        if self.status_callback is not None:
            self.status_callback(message)

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("Launch session is already running")
        runtime_env = capture_runtime_env(self.driver_setup)
        command = self.command()
        self._emit_status(f"starting launch: {' '.join(command)}")
        ensure_dir(self.log_path.parent)
        self._log_stream = self.log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            cwd=self.work_dir,
            env=runtime_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.root_pid = self.process.pid
        try:
            self.process_group_id = os.getpgid(self.process.pid)
        except OSError:
            self.process_group_id = None
        self._emit_status(f"launch process started, root pid={self.root_pid}")
        if self.process_group_id is not None:
            self._emit_status(
                f"launch process group created, pgid={self.process_group_id}"
            )
        self._reader_thread = threading.Thread(target=self._consume_output, daemon=True)
        self._reader_thread.start()

    def _consume_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            self._captured_lines.append(line.rstrip("\n"))
            self._log_stream.write(line)
            self._log_stream.flush()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def assert_running(self) -> None:
        if self.is_running():
            return
        output_tail = "\n".join(self._captured_lines[-40:])
        exit_code = None if self.process is None else self.process.poll()
        raise RuntimeError(
            f"Launch exited early for {self.launch_file} with code {exit_code}\n{output_tail}"
        )

    def stop(self, timeout: float = 10.0) -> None:
        if self.process is not None and self.process.poll() is None:
            self._emit_status(
                f"stopping launch process pid={self.process.pid} for {self.launch_file}"
            )
        elif self.process_group_id is not None:
            self._emit_status(
                f"stopping lingering launch process group pgid={self.process_group_id} for {self.launch_file}"
            )
        if self.process_group_id is not None or (
            self.process is not None and self.process.poll() is None
        ):
            self._terminate_process_group(timeout=timeout)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
        if self.root_pid is not None:
            self._emit_status(f"launch session closed for {self.launch_file}")
        self.process = None
        self.process_group_id = None

    def _terminate_process_group(self, timeout: float) -> None:
        if self.process is None and self.process_group_id is None:
            return

        pgid = self.process_group_id
        process_running = self.process is not None and self.process.poll() is None
        if pgid is None and self.process is not None:
            try:
                pgid = os.getpgid(self.process.pid)
            except OSError:
                pgid = None

        if pgid is None:
            if self.process is None or self.process.poll() is not None:
                return
            self._emit_status(
                "launch process group unavailable, falling back to direct process termination"
            )
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
            return

        self._signal_process_group(
            pgid,
            signal.SIGINT,
            "sending SIGINT to launch process group",
        )
        if not process_running:
            time.sleep(1.0)
            self._signal_process_group(
                pgid,
                signal.SIGTERM,
                "sending SIGTERM to lingering launch process group",
            )
            time.sleep(1.0)
            self._signal_process_group(
                pgid,
                signal.SIGKILL,
                "sending SIGKILL to lingering launch process group",
            )
            return
        try:
            self.process.wait(timeout=min(timeout, 5.0))
            return
        except subprocess.TimeoutExpired:
            self._emit_status(
                f"launch process group pgid={pgid} still running after SIGINT"
            )

        self._signal_process_group(
            pgid,
            signal.SIGTERM,
            "sending SIGTERM to launch process group",
        )
        try:
            self.process.wait(timeout=max(timeout - min(timeout, 5.0), 3.0))
            return
        except subprocess.TimeoutExpired:
            self._emit_status(
                f"launch process group pgid={pgid} still running after SIGTERM"
            )

        self._signal_process_group(
            pgid,
            signal.SIGKILL,
            "sending SIGKILL to launch process group",
        )
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._emit_status(
                f"launch process group pgid={pgid} did not exit even after SIGKILL"
            )

    def _signal_process_group(self, pgid: int, sig: int, message: str) -> None:
        try:
            self._emit_status(f"{message} pgid={pgid}")
            os.killpg(pgid, sig)
        except ProcessLookupError:
            self._emit_status(f"launch process group pgid={pgid} is already gone")
        except OSError as exc:
            self._emit_status(
                f"failed to signal launch process group pgid={pgid}: {exc}"
            )

    def pid_tree(self) -> Set[int]:
        if self.root_pid is None:
            return set()
        try:
            root = psutil.Process(self.root_pid)
        except psutil.Error:
            return set()
        pids = {root.pid}
        for child in root.children(recursive=True):
            pids.add(child.pid)
        return pids

    def _get_process(self, pid: int) -> Optional[psutil.Process]:
        try:
            process = self._process_cache.get(pid)
            if process is None:
                process = psutil.Process(pid)
                self._process_cache[pid] = process
            return process
        except psutil.Error:
            self._process_cache.pop(pid, None)
            self._primed_pids.discard(pid)
            return None

    def _matches_camera_process(self, process: psutil.Process) -> bool:
        try:
            name = (process.name() or "").lower()
            cmdline = " ".join(process.cmdline() or []).lower()
        except psutil.Error:
            return False
        return any(hint in name or hint in cmdline for hint in CAMERA_PROCESS_HINTS)

    def camera_pid_tree(self) -> Set[int]:
        active_pids = self.pid_tree()
        if not active_pids:
            return set()

        candidate_pids: Set[int] = set()
        for pid in sorted(active_pids):
            process = self._get_process(pid)
            if process is not None and self._matches_camera_process(process):
                candidate_pids.add(pid)

        if not candidate_pids:
            return set()

        root_candidates: Set[int] = set(candidate_pids)
        for pid in sorted(candidate_pids):
            process = self._get_process(pid)
            if process is None:
                continue
            try:
                parent = process.parent()
            except psutil.Error:
                parent = None
            while parent is not None:
                if parent.pid in candidate_pids:
                    root_candidates.discard(pid)
                    break
                if parent.pid not in active_pids:
                    break
                try:
                    parent = parent.parent()
                except psutil.Error:
                    break

        pids: Set[int] = set()
        for pid in sorted(root_candidates):
            process = self._get_process(pid)
            if process is None:
                continue
            pids.add(process.pid)
            try:
                for child in process.children(recursive=True):
                    if child.pid in active_pids:
                        pids.add(child.pid)
            except psutil.Error:
                continue
        return pids

    def _sample_pid_values(self, pid_values: list[int]) -> Dict[str, Any]:
        total_cpu = 0.0
        total_rss = 0
        active_pids = set(pid_values)

        for pid in list(self._process_cache):
            if pid not in active_pids:
                self._process_cache.pop(pid, None)
                self._primed_pids.discard(pid)

        for pid in pid_values:
            process = self._get_process(pid)
            if process is None:
                continue
            try:
                if pid not in self._primed_pids:
                    process.cpu_percent(interval=None)
                    self._primed_pids.add(pid)
                else:
                    total_cpu += process.cpu_percent(interval=None)
                total_rss += process.memory_info().rss
            except psutil.Error:
                self._process_cache.pop(pid, None)
                self._primed_pids.discard(pid)
                continue
        cpu_count = max(psutil.cpu_count() or 1, 1)
        return {
            "pid_count": len(pid_values),
            "pids": pid_values,
            "cpu_percent": total_cpu / cpu_count,
            "memory_rss_mb": total_rss / (1024.0 * 1024.0),
        }

    def sample_process_tree(self) -> Dict[str, Any]:
        return self._sample_pid_values(sorted(self.pid_tree()))

    def sample_camera_process_tree(self) -> Dict[str, Any]:
        return self._sample_pid_values(sorted(self.camera_pid_tree()))
