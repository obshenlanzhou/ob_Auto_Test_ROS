from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path


ZIP_REMOTE = "https://gitee.com/kong-yue1/ob_Auto_Test_ROS.git"


class UpdateError(RuntimeError):
    pass


class RepositoryUpdate:
    """Fast-forward the test tool checkout to its configured upstream branch."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._archive_lock = threading.Lock()
        self._archive_status = {"state": "unavailable", "message": "等待初始化 ZIP 工作区。"}

    def _git(self, *args: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", SSH_ASKPASS_REQUIRE="never")
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "credential.interactive=never", *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )

    def _is_archive(self) -> bool:
        return not os.path.lexists(self.root / ".git") and (
            self.root / "auto_test_ws" / "run_camera_auto_test_ui.sh"
        ).is_file()

    def initialize_archive(self) -> None:
        if not self._is_archive() or shutil.which("git") is None:
            return
        with self._archive_lock:
            if not self._is_archive():
                return
            self._archive_status = {"state": "initializing", "message": "正在从 Gitee 识别 ZIP 版本…"}
            try:
                self._attach_archive_metadata()
            except UpdateError as exc:
                message = str(exc)
            except subprocess.TimeoutExpired:
                message = "初始化 ZIP 工作区超时，请检查网络后重试。"
            except OSError:
                message = "无法写入 Git 元数据，请检查解压目录的权限和磁盘空间。"
            else:
                message = "ZIP 工作区已初始化。"
            self._archive_status = {"state": "unavailable", "message": message}

    def _attach_archive_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".orbbec-zip-", dir=self.root) as folder:
            clone = self._git("clone", "--no-checkout", "--single-branch", "--branch", "main",
                              "--no-tags", "--", ZIP_REMOTE, folder, timeout=60)
            if clone.returncode:
                raise UpdateError("无法从 Gitee 获取 ZIP 对应的版本，请检查网络后重试。")
            metadata = Path(folder) / ".git"
            # A shutdown during cloning can leave an older staging directory behind.
            exclude = metadata / "info" / "exclude"
            exclude.parent.mkdir(exist_ok=True)
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write("\n/.orbbec-zip-*/\n")

            def git(*args: str) -> str:
                result = self._git(f"--git-dir={metadata}", f"--work-tree={self.root}",
                                   "-c", "core.hooksPath=/dev/null", *args, timeout=15)
                if result.returncode:
                    raise UpdateError("无法校验 ZIP 工作区，请检查目录权限后重试。")
                return result.stdout

            # Build a snapshot in the temporary index so user files stay intact.
            git("add", "--all", "--", ".")
            snapshot = git("write-tree").strip()
            history = [line.split() for line in git(
                "log", "--first-parent", "--format=%H %T", "refs/remotes/origin/main",
            ).splitlines()]
            revision = next((commit for commit, tree in history if tree == snapshot), "")
            if not revision:
                # Some ZIP extractors drop the executable bits. Compare paths and blobs
                # while retaining the distinction between regular files and symlinks.
                contents = _tree_contents(git("ls-tree", "-rz", snapshot))
                reviewed = set()
                for commit, tree in history:
                    if tree in reviewed:
                        continue
                    reviewed.add(tree)
                    if _tree_contents(git("ls-tree", "-rz", tree)) == contents:
                        revision = commit
                        git("config", "core.filemode", "false")
                        break
            if not revision:
                raise UpdateError("ZIP 文件与 Gitee 的提交不匹配。请保留此目录，并另行克隆仓库。")

            git("update-ref", "refs/heads/main", revision)
            git("read-tree", revision)
            if git("status", "--porcelain", "--untracked-files=normal"):
                raise UpdateError("初始化期间工作区文件发生变化，请重新检查更新。")
            destination = self.root / ".git"
            if not os.path.lexists(destination):
                metadata.rename(destination)

    def local(self) -> dict:
        unavailable = {"enabled": False, "state": "unavailable"}
        try:
            root = self._git("rev-parse", "--show-toplevel")
        except (OSError, subprocess.TimeoutExpired):
            return {**unavailable, "message": "无法运行 Git，请确认 Git 已安装。"}
        if root.returncode or Path(root.stdout.strip()).resolve() != self.root:
            if self._is_archive():
                status = self._archive_status
                return {**unavailable, **status, "retry_initialization": status["state"] != "initializing"}
            return {**unavailable, "message": "在线更新需要通过 Git 克隆测试工具仓库。"}

        current = self._git("rev-parse", "--verify", "HEAD").stdout.strip()
        branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        remote = self._git("config", "--get", f"branch.{branch}.remote").stdout.strip()
        ref = self._git("config", "--get", f"branch.{branch}.merge").stdout.strip()
        info = {"current": current, "branch": branch, "remote": remote, "ref": ref}
        if not current or not branch:
            return {**info, **unavailable, "message": "请切换到有提交记录的本地分支。"}
        if not remote or remote == "." or not ref.startswith("refs/heads/"):
            return {**info, **unavailable, "message": "当前分支需要配置远程跟踪分支。"}
        return {**info, "enabled": True, "state": "unchecked"}

    def _fetch(self, remote: str, ref: str) -> str:
        # Fetch into a unique ref so parallel Git commands cannot replace the result.
        temporary_ref = f"refs/orbbec-ui-update/{uuid.uuid4().hex}"
        args = ("fetch", "--no-tags", "--no-prune", "--no-recurse-submodules", "--", remote,
                f"{ref}:{temporary_ref}")
        try:
            remote_url = self._git("remote", "get-url", "--", remote).stdout.strip()
            try:
                fetched = self._git(*args, timeout=15)
            except subprocess.TimeoutExpired:
                fetched = None
            if not fetched or fetched.returncode:
                ssh_url = re.fullmatch(
                    r"(?:git@|ssh://git@)(github\.com|gitee\.com)[:/]([\w.-]+/[\w.-]+)", remote_url,
                )
                if ssh_url:
                    host, path = ssh_url.groups()
                    fetched = self._git("-c", f"url.https://{host}/{path}.insteadOf={remote_url}",
                                        *args, timeout=15)
            if fetched and fetched.returncode == 0:
                latest = self._git("rev-parse", "--verify", temporary_ref).stdout.strip()
                if latest:
                    return latest
        except subprocess.TimeoutExpired as exc:
            raise UpdateError("连接远程仓库超时。") from exc
        finally:
            self._git("update-ref", "-d", temporary_ref)
        raise UpdateError("无法获取远程提交，请检查网络、仓库地址和 Git 授权。")

    def _working_tree_blocker(self) -> str:
        git_dir = self._git("rev-parse", "--absolute-git-dir")
        if git_dir.returncode:
            raise UpdateError("无法读取 Git 仓库状态。")
        root = Path(git_dir.stdout.strip())
        if any((root / marker).exists() for marker in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer",
        )):
            return "请先完成当前 Git 操作。"
        status = self._git("status", "--porcelain", "--untracked-files=normal")
        if status.returncode:
            raise UpdateError("无法读取工作区状态。")
        return "工作区有本地改动，请处理后再更新。" if status.stdout else ""

    def check(self) -> dict:
        info = self.local()
        if info.get("retry_initialization"):
            self.initialize_archive()
            info = self.local()
        if not info["enabled"]:
            return info
        current = info["current"]
        latest = self._fetch(info["remote"], info["ref"])
        info["latest"] = latest
        if current == latest:
            return {**info, "state": "current", "message": "当前已是远端最新版本。"}
        blocker = self._working_tree_blocker()
        if blocker:
            return {**info, "state": "blocked", "message": blocker}
        if self._git("merge-base", "--is-ancestor", current, latest).returncode:
            return {**info, "state": "blocked", "message": "本地分支有远端未包含的提交，无法快进更新。"}
        return {**info, "state": "available", "message": "发现新版本，更新后会重启 Web UI。"}

    def apply(self, current: str | None, latest: str | None) -> str:
        if not current or not latest:
            raise UpdateError("请先检查更新。")
        info = self.check()
        if current != info.get("current") or latest != info.get("latest"):
            raise UpdateError("本地或远端版本已变化，请重新检查更新。")
        if info["state"] != "available":
            raise UpdateError(info["message"])
        local = self.local()
        if any(local.get(key) != info[key] for key in ("current", "branch", "remote", "ref")):
            raise UpdateError("本地分支已变化，请重新检查更新。")
        blocker = self._working_tree_blocker()
        if blocker:
            raise UpdateError(blocker)
        merged = self._git("-c", "merge.autostash=false", "-c", "core.hooksPath=/dev/null", "merge",
                           "--ff-only", "--no-edit", "--no-overwrite-ignore", latest, timeout=30)
        if merged.returncode:
            raise UpdateError("快进更新失败，请检查 Git 工作区后重试。")
        return latest


def _tree_contents(entries: str) -> dict:
    contents = {}
    for entry in entries.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, _, object_id = metadata.split()
            contents[path] = ("100644" if mode == "100755" else mode, object_id)
    return contents
