from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict


class ProcessTreeSampler:
    def __init__(self, session, csv_path: Path) -> None:
        self.session = session
        self.csv_path = csv_path
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["elapsed_seconds", "pid_count", "cpu_percent", "memory_rss_mb"])
        self._skip_first_sample = True
        self.sample_count = 0
        self.cpu_sum = 0.0
        self.memory_sum = 0.0
        self.min_cpu = None
        self.min_memory = None
        self.max_cpu = 0.0
        self.max_memory = 0.0

    def sample(self, elapsed_seconds: float) -> Dict[str, float]:
        snapshot = self.session.sample_camera_process_tree()
        if self._skip_first_sample:
            self._skip_first_sample = False
            return snapshot

        self.csv_writer.writerow(
            [
                f"{elapsed_seconds:.3f}",
                snapshot["pid_count"],
                f"{snapshot['cpu_percent']:.3f}",
                f"{snapshot['memory_rss_mb']:.3f}",
            ]
        )
        self.csv_file.flush()
        self.sample_count += 1
        self.cpu_sum += snapshot["cpu_percent"]
        self.memory_sum += snapshot["memory_rss_mb"]
        if self.min_cpu is None:
            self.min_cpu = snapshot["cpu_percent"]
        else:
            self.min_cpu = min(self.min_cpu, snapshot["cpu_percent"])
        if self.min_memory is None:
            self.min_memory = snapshot["memory_rss_mb"]
        else:
            self.min_memory = min(self.min_memory, snapshot["memory_rss_mb"])
        self.max_cpu = max(self.max_cpu, snapshot["cpu_percent"])
        self.max_memory = max(self.max_memory, snapshot["memory_rss_mb"])
        return snapshot

    def build_summary(self) -> Dict[str, float]:
        if self.sample_count == 0:
            return {
                "avg_cpu_percent": 0.0,
                "min_cpu_percent": 0.0,
                "max_cpu_percent": 0.0,
                "avg_memory_rss_mb": 0.0,
                "min_memory_rss_mb": 0.0,
                "max_memory_rss_mb": 0.0,
            }
        return {
            "avg_cpu_percent": self.cpu_sum / self.sample_count,
            "min_cpu_percent": self.min_cpu if self.min_cpu is not None else 0.0,
            "max_cpu_percent": self.max_cpu,
            "avg_memory_rss_mb": self.memory_sum / self.sample_count,
            "min_memory_rss_mb": self.min_memory if self.min_memory is not None else 0.0,
            "max_memory_rss_mb": self.max_memory,
        }

    def close(self) -> None:
        self.csv_file.close()
