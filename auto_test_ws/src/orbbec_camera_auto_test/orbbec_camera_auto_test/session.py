from __future__ import annotations

import os
import platform
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Set

import psutil

from .reporter import ensure_dir


ROS1_VARS = ("ROS_MASTER_URI", "ROS_ROOT", "ROS_PACKAGE_PATH", "ROS_DISTRO", "ROS_ETC_DIR")
ROS2_VARS = ("AMENT_PREFIX_PATH",)
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


def normalize_ros_version(ros_version: str | int | None) -> str:
    version = str(ros_version or "2").strip()
    if version not in {"1", "2"}:
        raise ValueError(f"unsupported ROS version: {ros_version}")
    return version


def default_ros_setup(ros_version: str | int | None = "2") -> str:
    version = normalize_ros_version(ros_version)
    if version == "1":
        return "/opt/ros/one/setup.bash"
    return "/opt/ros/humble/setup.bash"


def _bash_setup_variant(setup_file: str) -> str:
    path = Path(setup_file)
    if path.name == "setup.zsh":
        candidate = path.with_name("setup.bash")
        if candidate.is_file():
            return str(candidate)
    return setup_file


def _setup_workspace_root(setup_file: str) -> Path:
    path = Path(setup_file).resolve()
    if path.parent.name in {"devel", "install"}:
        return path.parent.parent
    return path.parent


def _orbbec_sdk_library_dirs(*setup_files: Optional[str]) -> list[str]:
    arch_dirs = ("x64", "arm64", "arm32")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch_dirs = ("x64", "arm64", "arm32")
    elif machine in {"aarch64", "arm64"}:
        arch_dirs = ("arm64", "x64", "arm32")

    found: list[str] = []
    for setup_file in setup_files:
        if not setup_file:
            continue
        root = _setup_workspace_root(setup_file)
        candidates = [
            root / "install" / "orbbec_camera" / "lib",
            root / "src" / "orbbec-ros-sdk" / "SDK" / "lib",
            root / "src" / "OrbbecSDK_ROS2" / "orbbec_camera" / "SDK" / "lib",
        ]
        for base in candidates:
            candidate_dirs = [base] if base.name == "lib" and any(base.glob("libOrbbecSDK.so*")) else []
            candidate_dirs.extend(base / arch for arch in arch_dirs)
            for candidate in candidate_dirs:
                if candidate.is_dir() and any(candidate.glob("libOrbbecSDK.so*")):
                    text = str(candidate)
                    if text not in found:
                        found.append(text)
    return found


def _prepend_path_value(current: str, additions: Iterable[str]) -> str:
    parts = [part for part in current.split(os.pathsep) if part] if current else []
    result = []
    for part in list(additions) + parts:
        if part not in result:
            result.append(part)
    return os.pathsep.join(result)


def sanitize_ros_env(
    source_env: Optional[Dict[str, str]] = None,
    ros_version: str | int | None = "2",
) -> Dict[str, str]:
    version = normalize_ros_version(ros_version)
    env = dict(source_env or os.environ)
    if version == "2":
        for var_name in ROS1_VARS:
            env.pop(var_name, None)
        remove_markers = ("/opt/ros/one",)
    else:
        for var_name in (*ROS2_VARS, "ROS_DISTRO", "ROS_ETC_DIR"):
            env.pop(var_name, None)
        remove_markers = ("/opt/ros/humble", "/opt/ros/foxy", "/opt/ros/galactic", "/opt/ros/iron", "/opt/ros/jazzy")

    for var_name in PATH_LIKE_VARS:
        value = env.get(var_name)
        if not value:
            continue
        env[var_name] = os.pathsep.join(
            part
            for part in value.split(os.pathsep)
            if not any(marker in part for marker in remove_markers)
        )
    env["ROS_VERSION"] = version
    env["PYTHONUNBUFFERED"] = "1"
    return env


def capture_runtime_env(
    driver_setup: Optional[str] = None,
    ros_version: str | int | None = "2",
    ros_setup: Optional[str] = None,
) -> Dict[str, str]:
    version = normalize_ros_version(ros_version)
    setup_file = _bash_setup_variant(ros_setup or default_ros_setup(version))
    if not Path(setup_file).is_file():
        raise FileNotFoundError(f"ROS setup file not found: {setup_file}")
    if driver_setup:
        driver_setup = _bash_setup_variant(driver_setup)
    if driver_setup and not Path(driver_setup).is_file():
        raise FileNotFoundError(f"Driver setup file not found: {driver_setup}")

    command_parts = [f"source {shlex.quote(setup_file)} >/dev/null 2>&1"]
    if driver_setup:
        command_parts.append(f"source {shlex.quote(driver_setup)} >/dev/null 2>&1")
    command = " && ".join(command_parts) + " && env -0"
    raw = subprocess.check_output(
        ["bash", "-lc", command],
        env=sanitize_ros_env(ros_version=version),
    )
    env = sanitize_ros_env(_parse_env_output(raw), ros_version=version)
    sdk_dirs = _orbbec_sdk_library_dirs(setup_file, driver_setup)
    if sdk_dirs:
        env["LD_LIBRARY_PATH"] = _prepend_path_value(env.get("LD_LIBRARY_PATH", ""), sdk_dirs)
    return env


def launch_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def discover_orbbec_devices(
    driver_setup: Optional[str] = None,
    ros_version: str | int | None = "2",
    ros_setup: Optional[str] = None,
) -> Dict[str, Any]:
    version = normalize_ros_version(ros_version)
    runtime_env = capture_runtime_env(driver_setup, ros_version=version, ros_setup=ros_setup)
    command = (
        ["rosrun", "orbbec_camera", "list_devices_node"]
        if version == "1"
        else ["ros2", "run", "orbbec_camera", "list_devices_node"]
    )
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
    if version == "1" and "Couldn't find executable named list_devices_node" in output:
        return {
            "success": True,
            "skipped": True,
            "device_count": -1,
            "message": "device discovery command is not available in this ROS1 workspace",
            "output": output,
            "returncode": result.returncode,
        }
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
        ros_version: str | int | None = "2",
        ros_setup: Optional[str] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.launch_file = launch_file
        self.launch_args = dict(launch_args)
        self.work_dir = ensure_dir(work_dir)
        self.log_path = log_path
        self.driver_setup = driver_setup
        self.ros_version = normalize_ros_version(ros_version)
        self.ros_setup = ros_setup
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
        command = (
            ["roslaunch", "orbbec_camera", self.launch_file]
            if self.ros_version == "1"
            else ["ros2", "launch", "orbbec_camera", self.launch_file]
        )
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
        runtime_env = capture_runtime_env(
            self.driver_setup,
            ros_version=self.ros_version,
            ros_setup=self.ros_setup,
        )
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

    def _process_text(self, pid: int) -> str:
        process = self._get_process(pid)
        if process is None:
            return ""
        try:
            name = process.name() or ""
            cmdline = " ".join(process.cmdline() or [])
        except psutil.Error:
            return ""
        return f"{name} {cmdline}".lower()

    def _pid_tree_from_roots(self, root_pids: Iterable[int], active_pids: Set[int]) -> Set[int]:
        pids: Set[int] = set()
        for pid in sorted(set(root_pids)):
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

    def process_pids_matching(self, *needles: str) -> Set[int]:
        active_pids = self.pid_tree()
        if not active_pids:
            return set()
        normalized_needles = [needle.lower() for needle in needles if needle]
        if not normalized_needles:
            return set()
        root_pids = {
            pid
            for pid in active_pids
            if any(needle in self._process_text(pid) for needle in normalized_needles)
        }
        return self._pid_tree_from_roots(root_pids, active_pids)

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

    def sample_pid_groups(self, pid_groups: Dict[str, Set[int]]) -> Dict[str, Dict[str, Any]]:
        active_pids: Set[int] = set()
        for pids in pid_groups.values():
            active_pids.update(pids)

        for pid in list(self._process_cache):
            if pid not in active_pids:
                self._process_cache.pop(pid, None)
                self._primed_pids.discard(pid)

        per_pid: Dict[int, Dict[str, Any]] = {}
        for pid in sorted(active_pids):
            process = self._get_process(pid)
            if process is None:
                continue
            try:
                if pid not in self._primed_pids:
                    process.cpu_percent(interval=None)
                    self._primed_pids.add(pid)
                    cpu_percent = 0.0
                else:
                    cpu_percent = process.cpu_percent(interval=None)
                per_pid[pid] = {
                    "cpu_percent": cpu_percent,
                    "memory_rss_mb": process.memory_info().rss / (1024.0 * 1024.0),
                }
            except psutil.Error:
                self._process_cache.pop(pid, None)
                self._primed_pids.discard(pid)
                continue

        cpu_count = max(psutil.cpu_count() or 1, 1)
        snapshots: Dict[str, Dict[str, Any]] = {}
        for group_name, pids in pid_groups.items():
            group_pids = sorted(pid for pid in pids if pid in per_pid)
            snapshots[group_name] = {
                "pid_count": len(group_pids),
                "pids": group_pids,
                "cpu_percent": (
                    sum(per_pid[pid]["cpu_percent"] for pid in group_pids) / cpu_count
                ),
                "memory_rss_mb": sum(per_pid[pid]["memory_rss_mb"] for pid in group_pids),
            }
        return snapshots

    def sample_process_tree(self) -> Dict[str, Any]:
        return self._sample_pid_values(sorted(self.pid_tree()))

    def sample_camera_process_tree(self) -> Dict[str, Any]:
        return self._sample_pid_values(sorted(self.camera_pid_tree()))

    def sample_camera_process_groups(self, camera_names: list[str]) -> Dict[str, Dict[str, Any]]:
        pid_groups: Dict[str, Set[int]] = {}
        for camera_name in camera_names:
            needles = [
                camera_name,
                f"/{camera_name}",
                f"__ns:=/{camera_name}",
                f"namespace:={camera_name}",
            ]
            pid_groups[camera_name] = self.process_pids_matching(*needles)
        return self.sample_pid_groups(pid_groups)

    def sample_named_container_tree(self, container_name: str) -> Dict[str, Any]:
        if not container_name:
            return self.sample_camera_process_tree()
        snapshots = self.sample_pid_groups(
            {"shared_container": self.process_pids_matching(container_name)}
        )
        return snapshots["shared_container"]
