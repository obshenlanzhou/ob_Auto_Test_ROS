from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "package_stress_results.py"


def load_module():
    spec = importlib.util.spec_from_file_location("package_stress_results", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def write_file(path: Path, text: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_result(root: Path, test_id: str, rounds: list[dict], *, ui: bool = False):
    spec = MODULE.TEST_SPECS[test_id]
    payload = {
        "test_id": test_id,
        "status": "passed",
        "details": {"status": "passed", spec.rounds_key: rounds},
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    write_file(root / "summary.md")
    write_file(root / "events.jsonl")
    write_file(root / "terminal.log")
    if ui:
        write_file(root / "ui_request.json")
        write_file(root / "ui_status.json")
        write_file(root / "ui_stdout.log")


def archive_names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return set(archive.getnames())


@pytest.mark.parametrize("test_id", sorted(MODULE.TEST_SPECS))
def test_packages_only_last_five_rounds(tmp_path, test_id):
    root = tmp_path / test_id
    spec = MODULE.TEST_SPECS[test_id]
    rounds = []
    for round_id in range(1, 8):
        image = write_file(root / "images" / f"round_{round_id}.png")
        rounds.append(
            {
                spec.round_id_key: round_id,
                "status": "failed" if round_id == 2 else "passed",
                "images": [{"path": str(image)}],
                "error": "",
            }
        )
        for template in spec.round_dirs:
            write_file(root / template.format(round_id) / "round.log")
    for shared_dir in spec.shared_dirs:
        write_file(root / shared_dir / "shared.log")
    write_result(root, test_id, rounds)

    names = archive_names(MODULE.package_results(root))

    assert f"{root.name}/images/round_2.png" not in names
    assert f"{root.name}/images/round_3.png" in names
    assert f"{root.name}/images/round_7.png" in names
    if spec.shared_dirs:
        assert f"{root.name}/logs/shared.log" in names


def test_round_string_cannot_include_the_whole_result_directory(tmp_path):
    root = tmp_path / "run"
    spec = MODULE.TEST_SPECS["launch_restart_stream_check"]
    rounds = []
    for round_id in range(1, 8):
        image = write_file(root / "images" / f"image_{round_id:04d}.png")
        write_file(root / "logs" / f"test_{round_id:04d}" / "camera.log")
        rounds.append(
            {
                spec.round_id_key: round_id,
                "status": "passed",
                "files": [str(image)],
                "error": "",
                "directory": str(root),
            }
        )
    write_file(root / "old-package.tar.gz")
    write_result(root, "launch_restart_stream_check", rounds)

    names = archive_names(MODULE.package_results(root, tmp_path / "package.tar.gz"))

    assert f"{root.name}/images/image_0001.png" not in names
    assert f"{root.name}/images/image_0002.png" not in names
    assert f"{root.name}/logs/test_0001/camera.log" not in names
    assert f"{root.name}/logs/test_0002/camera.log" not in names
    assert f"{root.name}/old-package.tar.gz" not in names
    assert f"{root.name}/images/image_0003.png" in names
    assert f"{root.name}/images/image_0007.png" in names


def test_packages_ui_files_and_overwrites_output(tmp_path):
    root = tmp_path / "ui_run"
    spec = MODULE.TEST_SPECS["firmware_update_stress_test"]
    write_file(root / spec.round_dirs[0].format(1) / "update.log")
    write_result(
        root,
        "firmware_update_stress_test",
        [{spec.round_id_key: 1, "status": "passed"}],
        ui=True,
    )
    output = tmp_path / "result.tar.gz"
    output.write_bytes(b"old")

    MODULE.package_results(root, output)
    names = archive_names(output)

    assert f"{root.name}/ui_request.json" in names
    assert f"{root.name}/ui_status.json" in names
    assert f"{root.name}/ui_stdout.log" in names


def test_ignores_missing_and_external_files(tmp_path):
    root = tmp_path / "run"
    external = write_file(tmp_path / "external.log")
    spec = MODULE.TEST_SPECS["stream_toggle_stress_test"]
    write_file(root / "logs" / "camera.log")
    write_result(
        root,
        "stream_toggle_stress_test",
        [
            {
                spec.round_id_key: 1,
                "status": "passed",
                "files": [str(external), str(root / "missing.png")],
            }
        ],
    )

    names = archive_names(MODULE.package_results(root))

    assert all("external.log" not in name for name in names)
    assert all("missing.png" not in name for name in names)


def test_rejects_running_or_unknown_results(tmp_path):
    running = tmp_path / "running"
    running.mkdir()
    (running / "result.json").write_text(
        json.dumps(
            {
                "test_id": "firmware_update_stress_test",
                "status": "running",
                "details": {"tests": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MODULE.PackagingError, match="not finished"):
        MODULE.package_results(running)

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    (unknown / "result.json").write_text(
        json.dumps({"status": "passed", "details": {}}), encoding="utf-8"
    )
    with pytest.raises(MODULE.PackagingError, match="unsupported"):
        MODULE.package_results(unknown)
