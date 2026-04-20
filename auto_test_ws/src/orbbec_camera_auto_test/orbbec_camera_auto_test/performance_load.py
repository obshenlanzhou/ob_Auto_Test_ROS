from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Optional

from .profile_loader import ExternalLoadSpec


_CPU_BURN_CODE = """
import signal
import sys
import time

signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(0))

while True:
    pass
"""


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
        cpu_count = os.cpu_count() or 1
        return max(1, cpu_count // 2)

    def start(self) -> None:
        if self.active or self.spec is None:
            return
        if self.spec.type.lower() != "cpu":
            self._emit(f"external load type '{self.spec.type}' is unsupported, skipping")
            return

        workers = self._resolved_workers()
        self._emit(f"starting external CPU load with {workers} worker(s)")
        for _ in range(workers):
            process = subprocess.Popen(
                [sys.executable, "-c", _CPU_BURN_CODE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes.append(process)
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
