from io import BytesIO
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbbec_camera_auto_test_ui.repository_update import RepositoryUpdate, UpdateError
from orbbec_camera_auto_test_ui import repository_update, server


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(root, name, content):
    (root / name).write_text(content)
    git(root, "add", name)
    git(root, "commit", "-m", content)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "symbolic-ref", "HEAD", "refs/heads/main")
    launcher = source / "auto_test_ws/run_camera_auto_test_ui.sh"
    launcher.parent.mkdir()
    launcher.write_text("#!/usr/bin/env bash\n")
    launcher.chmod(0o755)
    (source / ".gitignore").write_text("results/\n")
    git(source, "add", ".gitignore", "auto_test_ws")
    current = commit(source, "version.txt", "original")
    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", "--bare", str(source), str(mirror))
    local = tmp_path / "local"
    git(tmp_path, "clone", "--origin", "gitee", str(mirror), str(local))
    git(source, "remote", "add", "mirror", str(mirror))
    latest = commit(source, "version.txt", "latest")
    git(source, "push", "mirror", "main")
    return source, local, current, latest


def test_fast_forward_uses_the_configured_upstream(checkout):
    _, local, current, latest = checkout
    updater = RepositoryUpdate(local)

    info = updater.check()
    assert (info["state"], info["remote"], info["current"], info["latest"]) == (
        "available", "gitee", current, latest,
    )
    assert (local / "version.txt").read_text() == "original"
    assert updater.apply(current, latest) == latest
    assert updater.check()["state"] == "current"
    assert (local / "version.txt").read_text() == "latest"
    assert git(local, "status", "--porcelain", "--branch") == "## main...gitee/main"


def test_update_refuses_to_overwrite_working_tree(checkout):
    _, local, current, latest = checkout
    updater = RepositoryUpdate(local)
    (local / "version.txt").write_text("local draft")
    assert updater.check()["state"] == "blocked"
    with pytest.raises(UpdateError, match="本地改动"):
        updater.apply(current, latest)
    assert (local / "version.txt").read_text() == "local draft"
    assert git(local, "rev-parse", "HEAD") == current


def test_update_refuses_a_diverged_branch(checkout):
    _, local, _, latest = checkout
    current = commit(local, "version.txt", "local commit")
    updater = RepositoryUpdate(local)
    assert updater.check()["state"] == "blocked"
    with pytest.raises(UpdateError, match="无法快进"):
        updater.apply(current, latest)
    assert git(local, "rev-parse", "HEAD") == current


def test_update_requires_a_fresh_check_if_upstream_moves(checkout):
    source, local, current, latest = checkout
    updater = RepositoryUpdate(local)
    assert updater.check()["latest"] == latest
    newer = commit(source, "version.txt", "newer")
    git(source, "push", "mirror", "main")

    with pytest.raises(UpdateError, match="远端版本已变化"):
        updater.apply(current, latest)
    assert git(local, "rev-parse", "HEAD") == current
    assert updater.check()["latest"] == newer


def test_checkout_must_be_the_tool_repository_root(checkout):
    _, local, _, _ = checkout
    directory = local / "archive"
    directory.mkdir()
    assert RepositoryUpdate(directory).local()["enabled"] is False


def test_running_tests_block_updates(monkeypatch):
    monkeypatch.setattr(server.MANAGER, "has_active_job", lambda: True)
    with pytest.raises(UpdateError, match="当前测试结束"):
        server.apply_repository_update({})


@pytest.fixture
def archive(checkout, tmp_path, monkeypatch):
    source, _, current, latest = checkout
    monkeypatch.setattr(repository_update, "ZIP_REMOTE", str(tmp_path / "mirror"))
    data = subprocess.check_output(["git", "-C", str(source), "archive", "--format=zip", current])
    directory = tmp_path / "extracted"
    with ZipFile(BytesIO(data)) as zipped:
        zipped.extractall(directory)
    results = directory / "auto_test_ws/results"
    results.mkdir()
    (results / "ui_config.json").write_text('{"retained": true}')
    return directory, current, latest


def test_older_zip_initializes_in_place_and_can_update(archive):
    directory, current, latest = archive
    updater = RepositoryUpdate(directory)
    launcher = directory / "auto_test_ws/run_camera_auto_test_ui.sh"
    launcher_mode = launcher.stat().st_mode

    updater.initialize_archive()

    assert updater.local()["current"] == current
    assert updater.check()["latest"] == latest
    assert (directory / "version.txt").read_text() == "original"
    assert launcher.stat().st_mode == launcher_mode
    assert git(directory, "status", "--porcelain") == ""
    assert updater.apply(current, latest) == latest
    assert (directory / "auto_test_ws/results/ui_config.json").read_text() == '{"retained": true}'


def test_zip_with_local_edits_is_preserved_and_can_retry(archive):
    directory, current, latest = archive
    updater = RepositoryUpdate(directory)
    version = directory / "version.txt"
    version.write_text("edited after extraction")

    updater.initialize_archive()

    assert "不匹配" in updater.local()["message"]
    assert not (directory / ".git").exists()
    assert not list(directory.glob(".orbbec-zip-*"))
    assert version.read_text() == "edited after extraction"

    version.write_text("original")
    info = updater.check()
    assert (info["state"], info["current"], info["latest"]) == ("available", current, latest)


def test_zip_network_failure_can_retry(archive, tmp_path, monkeypatch):
    directory, current, latest = archive
    updater = RepositoryUpdate(directory)
    monkeypatch.setattr(repository_update, "ZIP_REMOTE", str(tmp_path / "unreachable"))

    updater.initialize_archive()

    assert "请检查网络" in updater.local()["message"]
    assert not (directory / ".git").exists()

    interrupted = directory / ".orbbec-zip-interrupted"
    interrupted.mkdir()
    (interrupted / "partial-clone").write_text("staging")
    monkeypatch.setattr(repository_update, "ZIP_REMOTE", str(tmp_path / "mirror"))
    info = updater.check()
    assert (info["current"], info["latest"]) == (current, latest)
    assert git(directory, "status", "--porcelain") == ""
