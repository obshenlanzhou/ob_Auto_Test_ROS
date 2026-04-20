from __future__ import annotations

import os
import signal
import shutil
import subprocess
from typing import Optional

import psutil

from .profile_loader import ExternalLoadSpec


class ExternalLoadController:
    def __init__(self, spec: Optional[ExternalLoadSpec], emit_status=None) -> None:
        self.spec = spec
        self.emit_status = emit_status
        self.processes: list[subprocess.Popen[bytes]] = []
        self.active = False

    def _emit(self, message: str) -> None:
        if self.emit_status is not None:
            self.emit_status(message)

    def _resolved_workers(self) -> int:
        if self.spec is None:
            return 0
        if self.spec.workers > 0:
            return self.spec.workers
        cpu_count = psutil.cpu_count(logical=True) or os.cpu_count() or 1
        return max(1, cpu_count)

    def start(self) -> None:
        if self.active or self.spec is None:
            return

        load_type = self.spec.type.lower()
        if load_type == "stress-ng":
            executable = shutil.which("stress-ng")
            if executable is None:
                raise RuntimeError("stress-ng is not installed or not found in PATH")

            args = list(self.spec.args)
            if not args:
                workers = self._resolved_workers()
                args = [
                    "--cpu",
                    str(workers),
                    "--cpu-load",
                    "50",
                    "--vm",
                    "1",
                    "--vm-bytes",
                    "50%",
                    "--io",
                    "4",
                    "--hdd",
                    "1",
                    "--hdd-bytes",
                    "512M",
                ]
            command = [executable, *args]
            self._emit(f"starting external stress-ng load: {' '.join(command)}")
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes.append(process)
        else:
            raise RuntimeError(
                f"external load type '{self.spec.type}' is unsupported; use 'stress-ng'"
            )
        self.active = True

    def stop(self) -> None:
        if not self.active and not self.processes:
            return
        self._emit("stopping external load")
        for process in self.processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError:
                process.terminate()

        for process in self.processes:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    continue

        self.processes.clear()
        self.active = False

    def update(self, elapsed_seconds: float, total_duration: float) -> None:
        if self.spec is None:
            return

        start_after = max(self.spec.start_after_seconds, 0.0)
        stop_after = self.spec.stop_after_seconds
        if stop_after <= 0.0:
            stop_after = total_duration
        stop_after = min(stop_after, total_duration)

        if not self.active and elapsed_seconds >= start_after:
            self.start()
        if self.active and elapsed_seconds >= stop_after:
            self.stop()

    def close(self) -> None:
        self.stop()
