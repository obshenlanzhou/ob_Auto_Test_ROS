from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path


class UpdateError(RuntimeError):
    pass


class RepositoryUpdate:
    """Fast-forward the test tool checkout to its configured upstream branch."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _git(self, *args: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", SSH_ASKPASS_REQUIRE="never")
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "credential.interactive=never", *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )

    def local(self) -> dict:
        unavailable = {"enabled": False, "state": "unavailable"}
        try:
            root = self._git("rev-parse", "--show-toplevel")
        except (OSError, subprocess.TimeoutExpired):
            return {**unavailable, "message": "无法运行 Git，请确认 Git 已安装。"}
        if root.returncode or Path(root.stdout.strip()).resolve() != self.root:
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
