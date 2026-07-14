from pathlib import Path

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
