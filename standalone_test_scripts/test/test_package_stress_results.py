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


def test_package_specs_match_ui_manifest_capabilities():
    declared = {
        manifest["id"]
        for path in ROOT.glob("*/ui_manifest.json")
        for manifest in [json.loads(path.read_text(encoding="utf-8"))]
        if manifest.get("package_results") is True
    }

    assert declared == set(MODULE.TEST_SPECS)


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
                "status": "passed",
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


@pytest.mark.parametrize("existing", [False, True])
def test_failed_package_does_not_publish_partial_archive(tmp_path, monkeypatch, existing):
    root = tmp_path / "run"
    write_result(root, "stream_toggle_stress_test", [])
    output = root / "result.tar.gz"
    if existing:
        output.write_bytes(b"previous archive")
    original_add = tarfile.TarFile.addfile

    def fail_after_write(archive, *args, **kwargs):
        original_add(archive, *args, **kwargs)
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "addfile", fail_after_write)
    with pytest.raises(MODULE.PackagingError, match="disk full"):
        MODULE.package_results(root, output)

    if existing:
        assert output.read_bytes() == b"previous archive"
    else:
        assert not output.exists()
    assert not list(root.glob(".package-*"))


def test_archive_extracts_all_bytes_and_has_valid_gzip_footer(tmp_path):
    import gzip

    root = tmp_path / "run"
    write_result(root, "stream_toggle_stress_test", [])
    log = write_file(root / "logs" / "camera.log", "camera output\n" * 100000)
    output = root / "result.tar.gz"
    MODULE.package_results(root, output)
    with gzip.open(output, "rb") as stream:
        while stream.read(1024 * 1024):
            pass
    with tarfile.open(output, "r:gz") as archive:
        assert archive.extractfile("run/logs/camera.log").read() == log.read_bytes()
        assert archive.extractfile("run/result.json").read() == (root / "result.json").read_bytes()


def test_byte_progress_updates_inside_large_file_and_completes_after_publish(tmp_path, monkeypatch):
    root = tmp_path / "run"
    write_result(root, "stream_toggle_stress_test", [])
    write_file(root / "logs" / "camera.log", "log line\n" * 400000)
    output = root / "result.tar.gz"
    updates = []
    ticks = iter(range(1000))
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: next(ticks))

    def progress(value):
        updates.append(value)
        if value["status"] == "completed":
            assert output.is_file()
            archive_names(output)
        else:
            assert not output.exists()

    MODULE.package_results(root, output, progress)
    inside_file = [p for p in updates if p["current_file"] == "logs/camera.log"]
    assert len({p["processed_bytes"] for p in inside_file}) >= 3
    processed = [p["processed_bytes"] for p in updates]
    assert processed == sorted(processed)
    assert updates[-2]["status"] == "finalizing"
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["processed_bytes"] == updates[-1]["total_bytes"]


@pytest.mark.parametrize("test_id", sorted(MODULE.TEST_SPECS))
def test_packages_failure_windows_instead_of_only_tail(tmp_path, test_id):
    root = tmp_path / test_id
    spec = MODULE.TEST_SPECS[test_id]
    rounds = []
    for number in range(1, 31):
        log = write_file(root / "rounds" / f"{number}.log")
        rounds.append({spec.round_id_key: number,
                       "status": "failed" if number in {2, 12, 15} else "passed",
                       "log": str(log)})
    write_result(root, test_id, rounds)
    output = MODULE.package_results(root)
    with tarfile.open(output) as archive:
        names = archive.getnames()
    # Overlapping windows merge; each log is archived once. No unrelated tail.
    selected = set(range(1, 21))
    assert {int(Path(name).stem) for name in names if "/rounds/" in name} == selected
    assert len(names) == len(set(names))


def write_events(root, completed=18, failure=10, active=19):
    events = [{"current": number, "phase": "failed-cycle" if number == failure else "completed-cycle"}
              for number in range(1, completed + 1)]
    events.append({"current": active, "phase": "running"})
    write_file(root / "events.jsonl", "".join(json.dumps(event) + "\n" for event in events))


@pytest.mark.parametrize("test_id", sorted(MODULE.TEST_SPECS))
def test_live_snapshot_selects_completed_failure_window_without_result_json(tmp_path, test_id):
    root = tmp_path / test_id
    spec = MODULE.TEST_SPECS[test_id]
    write_events(root)
    for number in range(1, 20):
        for template in spec.round_dirs:
            write_file(root / template.format(number) / "round.log", str(number))
    shared = write_file(root / "logs" / "camera.launch.log", "shared log prefix\n")
    with (root / "events.jsonl").open("ab") as stream:
        stream.write(b'{"current": 20')  # Concurrent writer's incomplete final line.
    output = MODULE.package_results(root, snapshot=True, test_id=test_id)
    assert output.name.endswith("_snapshot.tar.gz")
    with tarfile.open(output) as archive:
        metadata = json.load(archive.extractfile(f"{root.name}/packaging_snapshot.json"))
        assert metadata["selected_rounds"] == list(range(5, 16))
        assert metadata["failed_rounds"] == [10]
        names = set(archive.getnames())
        for number in range(1, 20):
            for template in spec.round_dirs:
                assert (f"{root.name}/{template.format(number)}/round.log" in names) == (5 <= number <= 15)
        if spec.shared_dirs:
            assert archive.extractfile(f"{root.name}/logs/camera.launch.log").read() == shared.read_bytes()
        events = archive.extractfile(f"{root.name}/events.jsonl").read()
        assert events.endswith(b"\n")
    assert not (root / "result.json").exists()


def test_snapshot_waits_for_failed_round_to_finish_and_keeps_available_following_rounds(tmp_path):
    root = tmp_path / "run"
    write_events(root, completed=10, failure=10, active=10)
    with pytest.raises(MODULE.PackagingError, match="尚无已结束"):
        MODULE.package_results(root, snapshot=True, test_id="stream_toggle_stress_test")
    write_events(root, completed=12, failure=10, active=13)
    output = MODULE.package_results(root, snapshot=True, test_id="stream_toggle_stress_test")
    with tarfile.open(output) as archive:
        metadata = json.load(archive.extractfile("run/packaging_snapshot.json"))
        assert metadata["selected_rounds"] == list(range(5, 13))


def test_snapshot_caps_growing_logs_at_captured_size(tmp_path):
    root = tmp_path / "run"
    write_events(root)
    log = write_file(root / "logs" / "camera.launch.log", "before\n")
    appended = False

    def progress(value):
        nonlocal appended
        if not appended:
            with log.open("a") as stream:
                stream.write("after\n" * 100)
            appended = True

    output = MODULE.package_results(root, progress=progress, snapshot=True,
                                    test_id="stream_toggle_stress_test")
    with tarfile.open(output) as archive:
        assert archive.extractfile("run/logs/camera.launch.log").read() == b"before\n"
    assert log.stat().st_size > len(b"before\n")
