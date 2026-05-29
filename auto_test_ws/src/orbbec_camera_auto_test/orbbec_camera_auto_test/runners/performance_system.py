from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List


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


class MultiCameraSystemSampler:
    def __init__(
        self,
        session,
        csv_path: Path,
        camera_names: List[str],
        resource_mode: str,
        container_name: str = "",
    ) -> None:
        self.session = session
        self.csv_path = csv_path
        self.camera_names = list(camera_names)
        self.resource_mode = resource_mode
        self.container_name = container_name
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(
            [
                "elapsed_seconds",
                "scope",
                "camera_name",
                "pid_count",
                "cpu_percent",
                "memory_rss_mb",
            ]
        )
        self._skip_first_sample = True
        self.stats: Dict[str, Dict[str, Any]] = {}

    def _record(self, key: str, snapshot: Dict[str, Any]) -> None:
        stats = self.stats.setdefault(
            key,
            {
                "sample_count": 0,
                "cpu_sum": 0.0,
                "memory_sum": 0.0,
                "min_cpu": None,
                "min_memory": None,
                "max_cpu": 0.0,
                "max_memory": 0.0,
            },
        )
        stats["sample_count"] += 1
        stats["cpu_sum"] += snapshot["cpu_percent"]
        stats["memory_sum"] += snapshot["memory_rss_mb"]
        stats["min_cpu"] = (
            snapshot["cpu_percent"]
            if stats["min_cpu"] is None
            else min(stats["min_cpu"], snapshot["cpu_percent"])
        )
        stats["min_memory"] = (
            snapshot["memory_rss_mb"]
            if stats["min_memory"] is None
            else min(stats["min_memory"], snapshot["memory_rss_mb"])
        )
        stats["max_cpu"] = max(stats["max_cpu"], snapshot["cpu_percent"])
        stats["max_memory"] = max(stats["max_memory"], snapshot["memory_rss_mb"])

    def _sample_snapshots(self) -> List[tuple[str, str, Dict[str, Any]]]:
        if self.resource_mode == "shared_container":
            camera_name = self.container_name or "shared_container"
            pids = (
                self.session.process_pids_matching(self.container_name)
                if self.container_name
                else self.session.camera_pid_tree()
            )
            snapshots = self.session.sample_pid_groups({"shared_container": pids})
            return [
                (
                    "shared_container",
                    camera_name,
                    snapshots["shared_container"],
                )
            ]

        pid_groups = {}
        row_keys = []
        for camera_name in self.camera_names:
            needles = [
                camera_name,
                f"/{camera_name}",
                f"__ns:=/{camera_name}",
                f"namespace:={camera_name}",
            ]
            key = f"camera:{camera_name}"
            pid_groups[key] = self.session.process_pids_matching(*needles)
            row_keys.append((key, "camera", camera_name))

        pid_groups["total:all"] = self.session.camera_pid_tree()
        row_keys.append(("total:all", "total", "all"))
        snapshots = self.session.sample_pid_groups(pid_groups)
        rows = [
            (scope, camera_name, snapshots[key])
            for key, scope, camera_name in row_keys
        ]
        return rows

    def sample(self, elapsed_seconds: float) -> List[tuple[str, str, Dict[str, Any]]]:
        rows = self._sample_snapshots()
        if self._skip_first_sample:
            self._skip_first_sample = False
            return rows

        for scope, camera_name, snapshot in rows:
            self.csv_writer.writerow(
                [
                    f"{elapsed_seconds:.3f}",
                    scope,
                    camera_name,
                    snapshot["pid_count"],
                    f"{snapshot['cpu_percent']:.3f}",
                    f"{snapshot['memory_rss_mb']:.3f}",
                ]
            )
            self._record(f"{scope}:{camera_name}", snapshot)
        self.csv_file.flush()
        return rows

    def build_summary(self) -> Dict[str, Dict[str, float]]:
        summary: Dict[str, Dict[str, float]] = {}
        for key, stats in self.stats.items():
            sample_count = stats["sample_count"]
            if sample_count <= 0:
                summary[key] = {
                    "avg_cpu_percent": 0.0,
                    "min_cpu_percent": 0.0,
                    "max_cpu_percent": 0.0,
                    "avg_memory_rss_mb": 0.0,
                    "min_memory_rss_mb": 0.0,
                    "max_memory_rss_mb": 0.0,
                }
                continue
            summary[key] = {
                "avg_cpu_percent": stats["cpu_sum"] / sample_count,
                "min_cpu_percent": stats["min_cpu"] if stats["min_cpu"] is not None else 0.0,
                "max_cpu_percent": stats["max_cpu"],
                "avg_memory_rss_mb": stats["memory_sum"] / sample_count,
                "min_memory_rss_mb": (
                    stats["min_memory"] if stats["min_memory"] is not None else 0.0
                ),
                "max_memory_rss_mb": stats["max_memory"],
            }
        return summary

    def close(self) -> None:
        self.csv_file.close()
