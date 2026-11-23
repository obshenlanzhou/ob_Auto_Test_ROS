import os
from pathlib import Path
import subprocess

import pytest

from orbbec_camera_auto_test.core import transport_build


def test_transport_cache_is_isolated_by_ros_setup_and_reuses_completed_builds(tmp_path, monkeypatch):
    build_calls = []

    def simulate_build(command, **kwargs):
        build_calls.append(command)
        install = Path(command[command.index("--install-base") + 1])
        executable = install / "lib" / transport_build.TRANSPORT_PACKAGE / "transport_subscriber"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        (install / "local_setup.bash").touch()

    monkeypatch.setattr(transport_build, "capture_runtime_env", lambda **kwargs: {"ROS_DISTRO": "jazzy"})
    monkeypatch.setattr(transport_build.subprocess, "run", simulate_build)
    first_setup = str(tmp_path / "ros-one" / "setup.bash")
    second_setup = str(tmp_path / "ros-two" / "setup.bash")

    first = transport_build.ensure_transport_setup(tmp_path, first_setup, refresh=True)
    assert transport_build.ensure_transport_setup(tmp_path, first_setup) == first
    assert len(build_calls) == 1
    ready = next((tmp_path / "build").rglob(".ready"))
    changed_at = ready.stat().st_mtime_ns + 1
    source = tmp_path / "src" / transport_build.TRANSPORT_PACKAGE / "src" / "probe.cpp"
    source.parent.mkdir(parents=True)
    source.touch()
    os.utime(source, ns=(changed_at, changed_at))
    assert transport_build.ensure_transport_setup(tmp_path, first_setup) == first
    assert len(build_calls) == 2
    assert transport_build.ensure_transport_setup(tmp_path, second_setup) != first
    assert len(build_calls) == 3


def test_interrupted_refresh_is_not_cached(tmp_path, monkeypatch):
    def simulate_build(command, **kwargs):
        install = Path(command[command.index("--install-base") + 1])
        executable = install / "lib" / transport_build.TRANSPORT_PACKAGE / "transport_subscriber"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        (install / "local_setup.bash").touch()

    def fail_build(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(transport_build, "capture_runtime_env", lambda **kwargs: {"ROS_DISTRO": "jazzy"})
    monkeypatch.setattr(transport_build.subprocess, "run", simulate_build)
    ros_setup = str(tmp_path / "setup.bash")
    transport_build.ensure_transport_setup(tmp_path, ros_setup)

    monkeypatch.setattr(transport_build.subprocess, "run", fail_build)
    with pytest.raises(subprocess.CalledProcessError):
        transport_build.ensure_transport_setup(tmp_path, ros_setup, refresh=True)
    with pytest.raises(subprocess.CalledProcessError):
        transport_build.ensure_transport_setup(tmp_path, ros_setup)
