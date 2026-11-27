"""Prepare the transport test component for a selected ROS 2 environment."""

from __future__ import annotations

import fcntl
import hashlib
import platform
import subprocess
from pathlib import Path

from .session import capture_runtime_env

TRANSPORT_PACKAGE = "orbbec_camera_transport_test"


def ensure_transport_setup(workspace: Path, ros_setup: str, *, refresh: bool = False,
                           driver_setup: str | None = None, backend: str = "NONE") -> str:
    ros_setup = str(Path(ros_setup).expanduser().resolve())
    env = capture_runtime_env(ros_setup=ros_setup, driver_setup=driver_setup)
    distro = env.get("ROS_DISTRO")
    if not distro:
        raise RuntimeError(f"Unable to determine the ROS 2 distribution from {ros_setup}")

    underlay_key = ros_setup
    if backend != "NONE":
        underlay_key += f"\n{backend}\n{env.get('AMENT_PREFIX_PATH', '')}"
    underlay = hashlib.sha256(underlay_key.encode()).hexdigest()[:12]
    cache_key = f"{distro}-{platform.machine()}-{underlay}"
    source = workspace / "src" / TRANSPORT_PACKAGE
    build = workspace / "build" / "transport" / cache_key
    install = workspace / "install" / "transport" / cache_key
    log = workspace / "log" / "transport" / cache_key
    setup = install / "local_setup.bash"
    subscriber = install / "lib" / TRANSPORT_PACKAGE / "transport_subscriber"
    ready = build / ".ready"
    build.mkdir(parents=True, exist_ok=True)

    with (build / ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if ready.is_file() and setup.is_file() and subscriber.is_file() and not refresh:
            built_at = ready.stat().st_mtime_ns
            inputs = [
                source / "CMakeLists.txt",
                source / "package.xml",
                *source.glob("src/**/*"),
                *source.glob("launch/**/*.launch.py"),
            ]
            if all(path.stat().st_mtime_ns <= built_at for path in inputs if path.is_file()):
                return str(setup)

        ready.unlink(missing_ok=True)
        if not Path("/etc/nv_tegra_release").is_file():
            env.setdefault("MAKEFLAGS", "-j2")
        print(f"[Transport] Preparing {TRANSPORT_PACKAGE} for ROS 2 {distro}", flush=True)
        subprocess.run(
            [
                "colcon", "--log-base", str(log), "build",
                "--base-paths", str(source),
                "--build-base", str(build), "--install-base", str(install),
                "--merge-install", "--packages-select", TRANSPORT_PACKAGE,
                "--cmake-args", f"-DTRANSPORT_GPU_BACKEND={backend}",
            ],
            cwd=workspace, env=env, check=True,
        )
        if not (setup.is_file() and subscriber.is_file()):
            raise RuntimeError(f"Cannot find the installed transport test under {install}")
        ready.touch()
        return str(setup)
