from pathlib import Path

import pytest

from orbbec_camera_auto_test.core import session
from orbbec_camera_auto_test.core.session import _orbbec_sdk_library_dirs


def _create_sdk_library(directory: Path) -> str:
    directory.mkdir(parents=True)
    (directory / "libOrbbecSDK.so.2").touch()
    return str(directory)


def test_devel_setup_prioritizes_ros1_source_sdk(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    setup_file = workspace / "devel" / "setup.bash"
    setup_file.parent.mkdir(parents=True)
    setup_file.touch()
    source_sdk = _create_sdk_library(
        workspace / "src" / "OrbbecSDK_ROS1" / "SDK" / "lib" / "x64"
    )
    install_sdk = _create_sdk_library(workspace / "install" / "orbbec_camera" / "lib")

    assert _orbbec_sdk_library_dirs(str(setup_file)) == [source_sdk, install_sdk]


def test_install_setup_prioritizes_install_sdk(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    setup_file = workspace / "install" / "setup.bash"
    setup_file.parent.mkdir(parents=True)
    setup_file.touch()
    source_sdk = _create_sdk_library(
        workspace / "src" / "OrbbecSDK_ROS1" / "SDK" / "lib" / "x64"
    )
    install_sdk = _create_sdk_library(workspace / "install" / "orbbec_camera" / "lib")

    assert _orbbec_sdk_library_dirs(str(setup_file)) == [install_sdk, source_sdk]


@pytest.mark.parametrize(
    ("ros_version", "expected_ros_home"),
    [("1", "work_dir"), ("2", None)],
)
def test_session_sets_ros_home_for_ros1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ros_version: str,
    expected_ros_home: str | None,
) -> None:
    captured_env = {}

    class FakeProcess:
        pid = 123
        stdout = []

    def fake_popen(*args, **kwargs):
        del args
        captured_env.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(session, "capture_runtime_env", lambda *args, **kwargs: {})
    monkeypatch.setattr(session.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(session.os, "getpgid", lambda pid: pid)
    work_dir = tmp_path / "work_dir"
    test_session = session.TestSession(
        launch_file="camera.launch",
        launch_args={},
        work_dir=work_dir,
        log_path=tmp_path / "launch.log",
        ros_version=ros_version,
    )

    test_session.start()

    if expected_ros_home is None:
        assert "ROS_HOME" not in captured_env
    else:
        assert captured_env["ROS_HOME"] == str(work_dir)


def test_session_detects_fatal_driver_initialization_output(tmp_path: Path) -> None:
    class RunningProcess:
        @staticmethod
        def poll():
            return None

    test_session = session.TestSession(
        launch_file="camera.launch.py",
        launch_args={},
        work_dir=tmp_path / "work",
        log_path=tmp_path / "launch.log",
    )
    test_session.process = RunningProcess()
    test_session._captured_lines.append(  # noqa: SLF001
        "[camera] [ERROR] Failed to initialize device accel sensor not found"
    )

    with pytest.raises(RuntimeError, match="fatal startup error"):
        test_session.assert_healthy()


def test_session_treats_ros_error_log_as_fatal(tmp_path: Path) -> None:
    class RunningProcess:
        @staticmethod
        def poll():
            return None

    test_session = session.TestSession(
        launch_file="camera.launch.py",
        launch_args={},
        work_dir=tmp_path / "work",
        log_path=tmp_path / "launch.log",
    )
    test_session.process = RunningProcess()
    test_session._captured_lines.append(  # noqa: SLF001
        "[camera] [ERROR] recoverable frame warning"
    )

    with pytest.raises(RuntimeError, match="recoverable frame warning"):
        test_session.assert_healthy()
