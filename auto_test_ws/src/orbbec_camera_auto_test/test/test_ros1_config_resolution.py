from pathlib import Path
from unittest import mock
import tempfile
import unittest

from orbbec_camera_auto_test.core.session import TestSession


class Ros1ConfigResolutionTest(unittest.TestCase):
    def test_bare_config_name_resolves_from_orbbec_camera_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = Path(temp_dir) / "orbbec_camera"
            config_file = package_dir / "config" / "gemini2L_dual_ir.yaml"
            config_file.parent.mkdir(parents=True)
            config_file.touch()
            session = TestSession(
                launch_file="gemini2L.launch",
                launch_args={"config_file_path": config_file.name},
                work_dir=Path(temp_dir) / "work",
                log_path=Path(temp_dir) / "launch.log",
                ros_version="1",
            )
            with mock.patch(
                "orbbec_camera_auto_test.core.session.subprocess.check_output",
                return_value=str(package_dir),
            ) as check_output:
                session._resolve_ros1_config_file({"PATH": "/usr/bin"})

            self.assertEqual(session.launch_args["config_file_path"], str(config_file))
            check_output.assert_called_once()


if __name__ == "__main__":
    unittest.main()
