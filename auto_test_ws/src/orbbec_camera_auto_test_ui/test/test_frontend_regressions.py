from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend regression tests")
    completed = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("frontend_regressions.cjs"))],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
