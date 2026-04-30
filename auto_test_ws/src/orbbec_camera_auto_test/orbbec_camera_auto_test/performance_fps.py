from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .profile_loader import TopicSpec
from .ros_utils import resolve_message_type

_TOPIC_FPS_KEY_RULES = (
    ("/color/image_raw", "color_fps"),
    ("/depth/image_raw", "depth_fps"),
    ("/depth/points", "depth_fps"),
    ("/left_ir/image_raw", "left_ir_fps"),
    ("/right_ir/image_raw", "right_ir_fps"),
    ("/ir/image_raw", "ir_fps"),
)


def _topic_label(topic_name: str) -> str:
    return topic_name.strip("/").replace("/", "_")


def _header_stamp(message: Any) -> Optional[float]:
    header = getattr(message, "header", None)
    if header is None or getattr(header, "stamp", None) is None:
        return None
    sec = getattr(header.stamp, "sec", getattr(header.stamp, "secs", 0))
    nanosec = getattr(header.stamp, "nanosec", getattr(header.stamp, "nsecs", 0))
    stamp = sec + nanosec * 1e-9
    if stamp <= 0.0:
        return None
    return stamp


def _infer_ideal_fps_key(topic_name: str) -> Optional[str]:
    normalized = topic_name.rstrip("/")
    for suffix, fps_key in _TOPIC_FPS_KEY_RULES:
        if normalized.endswith(suffix):
            return fps_key
    return None


def _coerce_positive_float(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric > 0.0 else 0.0


def _resolve_ideal_fps(spec: TopicSpec, launch_args: Dict[str, Any]) -> tuple[Optional[str], float]:
    if spec.ideal_fps > 0.0:
        return spec.ideal_fps_key, spec.ideal_fps
    fps_key = spec.ideal_fps_key or _infer_ideal_fps_key(spec.name)
    if not fps_key:
        return None, 0.0
    return fps_key, _coerce_positive_float(launch_args.get(fps_key))


def _estimate_missing_count(delta_seconds: float, ideal_fps: float) -> int:
    if ideal_fps <= 0.0 or delta_seconds <= 0.0:
        return 0
    expected_interval = 1.0 / ideal_fps
    estimated_frame_span = int((delta_seconds / expected_interval) + 0.5)
    return max(0, estimated_frame_span - 1)


def _fps_from_delta(total_delta: float, pair_count: int) -> float:
    if total_delta <= 0.0 or pair_count <= 0:
        return 0.0
    return pair_count / total_delta


def _event_rate(event_count: int, message_count: int) -> float:
    total = message_count + event_count
    if total <= 0:
        return 0.0
    return event_count / total


class TopicFpsCollector:
    def __init__(
        self,
        node,
        topic_specs: List[TopicSpec],
        csv_path: Path,
        launch_args: Dict[str, Any],
        sample_period: float = 1.0,
        frame_timestamp_dir: Optional[Path] = None,
        frame_timestamp_flush_every_rows: int = 1000,
        ros_version: str = "2",
    ):
        self.node = node
        self.topic_specs = topic_specs
        self.csv_path = csv_path
        self.sample_period = sample_period
        self.start_time = time.monotonic()
        self.launch_args = dict(launch_args)
        self.trackers: Dict[str, Dict[str, Any]] = {}
        self.subscriptions = []
        self.frame_timestamp_dir = frame_timestamp_dir
        self.frame_timestamp_flush_every_rows = max(frame_timestamp_flush_every_rows, 1)
        self.ros_version = str(ros_version)
        self.frame_timestamp_files: Dict[str, Any] = {}
        self.frame_timestamp_writers: Dict[str, csv.writer] = {}
        self.frame_timestamp_rows_since_flush: Dict[str, int] = {}

        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        if self.frame_timestamp_dir is not None:
            self.frame_timestamp_dir.mkdir(parents=True, exist_ok=True)

        header = ["elapsed_seconds"]
        for spec in topic_specs:
            label = _topic_label(spec.name)
            fps_key, ideal_fps = _resolve_ideal_fps(spec, self.launch_args)
            header.extend(
                [
                    f"{label}_ideal_fps",
                    f"{label}_current_fps",
                    f"{label}_avg_fps",
                    f"{label}_dropped_frames",
                    f"{label}_drop_rate",
                ]
            )
            self.trackers[spec.name] = {
                "ideal_fps_key": fps_key,
                "ideal_fps": ideal_fps,
                "ideal_fps_explicit": spec.ideal_fps > 0.0,
                "message_count": 0,
                "invalid_stamp_count": 0,
                "current_fps": 0.0,
                "prev_stamp": None,
                "prev_recv_monotonic": None,
                "window_pair_count": 0,
                "window_delta_sum": 0.0,
                "total_pair_count": 0,
                "total_delta_sum": 0.0,
                "total_dropped_frames": 0,
            }
            self._open_frame_timestamp_file(spec.name)
            msg_type = resolve_message_type(spec.type, self.ros_version)
            self.subscriptions.append(
                self.node.create_subscription(
                    msg_type,
                    spec.name,
                    lambda msg, topic_name=spec.name: self._on_message(topic_name, msg),
                    10,
                )
            )
        self.csv_writer.writerow(header)
        self.timer = self.node.create_timer(self.sample_period, self._on_sample)

    def _open_frame_timestamp_file(self, topic_name: str) -> None:
        if self.frame_timestamp_dir is None:
            return
        label = _topic_label(topic_name)
        self.frame_timestamp_dir.mkdir(parents=True, exist_ok=True)
        path = self.frame_timestamp_dir / f"{label}.csv"
        csv_file = path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "header_stamp",
                "recv_monotonic",
                "delta_header_ms",
                "delta_recv_ms",
                "estimated_drop",
            ]
        )
        self.frame_timestamp_files[topic_name] = csv_file
        self.frame_timestamp_writers[topic_name] = writer
        self.frame_timestamp_rows_since_flush[topic_name] = 0

    def _write_frame_timestamp(
        self,
        topic_name: str,
        tracker: Dict[str, Any],
        stamp: Optional[float],
        recv_monotonic: float,
        delta_header_seconds: Optional[float],
        delta_recv_seconds: Optional[float],
        estimated_drop: int,
    ) -> None:
        writer = self.frame_timestamp_writers.get(topic_name)
        if writer is None:
            return
        writer.writerow(
            [
                f"{stamp:.9f}" if stamp is not None else "",
                f"{recv_monotonic:.9f}",
                f"{delta_header_seconds * 1000.0:.3f}"
                if delta_header_seconds is not None
                else "",
                f"{delta_recv_seconds * 1000.0:.3f}"
                if delta_recv_seconds is not None
                else "",
                estimated_drop,
            ]
        )
        self.frame_timestamp_rows_since_flush[topic_name] += 1
        if (
            self.frame_timestamp_rows_since_flush[topic_name]
            >= self.frame_timestamp_flush_every_rows
        ):
            self.frame_timestamp_files[topic_name].flush()
            self.frame_timestamp_rows_since_flush[topic_name] = 0

    def describe_topics(self) -> List[str]:
        lines: List[str] = []
        for spec in self.topic_specs:
            tracker = self.trackers[spec.name]
            ideal_fps = tracker["ideal_fps"]
            ideal_fps_key = tracker["ideal_fps_key"]
            if tracker["ideal_fps_explicit"] and ideal_fps > 0.0:
                lines.append(
                    f"[PERF][TOPIC] {spec.name}: ideal_fps={ideal_fps:.3f} from topic configuration"
                )
            elif ideal_fps_key and ideal_fps > 0.0:
                lines.append(
                    f"[PERF][TOPIC] {spec.name}: ideal_fps={ideal_fps:.3f} from launch arg '{ideal_fps_key}'"
                )
            elif ideal_fps_key:
                lines.append(
                    f"[PERF][TOPIC] {spec.name}: launch arg '{ideal_fps_key}' is unset or <= 0, drop estimation disabled"
                )
            else:
                lines.append(
                    f"[PERF][TOPIC] {spec.name}: no ideal_fps mapping, drop estimation disabled"
                )
        return lines

    def _update_fps_stats(
        self, tracker: Dict[str, Any], delta_seconds: float, missing_count: int = 0
    ) -> None:
        if delta_seconds <= 0.0:
            return
        tracker["window_pair_count"] += 1
        tracker["window_delta_sum"] += delta_seconds
        tracker["total_pair_count"] += 1
        tracker["total_delta_sum"] += delta_seconds
        tracker["total_dropped_frames"] += missing_count

    def _on_message(self, topic_name: str, message: Any) -> None:
        tracker = self.trackers[topic_name]
        tracker["message_count"] += 1
        recv_monotonic = time.monotonic()
        prev_recv_monotonic = tracker["prev_recv_monotonic"]
        delta_recv_seconds = (
            recv_monotonic - prev_recv_monotonic
            if prev_recv_monotonic is not None
            else None
        )
        tracker["prev_recv_monotonic"] = recv_monotonic

        stamp = _header_stamp(message)
        if stamp is None:
            tracker["invalid_stamp_count"] += 1
            tracker["prev_stamp"] = None
            self._write_frame_timestamp(
                topic_name,
                tracker,
                None,
                recv_monotonic,
                None,
                delta_recv_seconds,
                0,
            )
            return

        delta_seconds = None
        dropped_frames = 0
        if tracker["prev_stamp"] is not None:
            delta_seconds = stamp - tracker["prev_stamp"]
            if delta_seconds <= 0.0:
                tracker["invalid_stamp_count"] += 1
            else:
                dropped_frames = _estimate_missing_count(delta_seconds, tracker["ideal_fps"])
                self._update_fps_stats(
                    tracker, delta_seconds, missing_count=dropped_frames
                )
        self._write_frame_timestamp(
            topic_name,
            tracker,
            stamp,
            recv_monotonic,
            delta_seconds if delta_seconds and delta_seconds > 0.0 else None,
            delta_recv_seconds,
            dropped_frames,
        )
        tracker["prev_stamp"] = stamp

    def _on_sample(self) -> None:
        elapsed = time.monotonic() - self.start_time
        row = [f"{elapsed:.3f}"]
        for spec in self.topic_specs:
            tracker = self.trackers[spec.name]
            current_fps = _fps_from_delta(
                tracker["window_delta_sum"],
                tracker["window_pair_count"],
            )
            avg_fps = _fps_from_delta(
                tracker["total_delta_sum"],
                tracker["total_pair_count"],
            )
            drop_rate = _event_rate(
                tracker["total_dropped_frames"], tracker["message_count"]
            )
            tracker["current_fps"] = current_fps
            row.extend(
                [
                    f"{tracker['ideal_fps']:.3f}",
                    f"{current_fps:.3f}",
                    f"{avg_fps:.3f}",
                    str(tracker["total_dropped_frames"]),
                    f"{drop_rate:.6f}",
                ]
            )
            tracker["window_pair_count"] = 0
            tracker["window_delta_sum"] = 0.0
        self.csv_writer.writerow(row)
        self.csv_file.flush()

    def build_summary(self) -> Dict[str, Dict[str, float]]:
        summary: Dict[str, Dict[str, float]] = {}
        for spec in self.topic_specs:
            tracker = self.trackers[spec.name]
            avg_fps = _fps_from_delta(
                tracker["total_delta_sum"],
                tracker["total_pair_count"],
            )
            summary[spec.name] = {
                "ideal_fps_key": tracker["ideal_fps_key"] or "",
                "ideal_fps": tracker["ideal_fps"],
                "current_fps": tracker["current_fps"],
                "avg_fps": avg_fps,
                "dropped_frames": tracker["total_dropped_frames"],
                "message_count": tracker["message_count"],
                "drop_rate": _event_rate(
                    tracker["total_dropped_frames"], tracker["message_count"]
                ),
            }
        return summary

    def close(self) -> None:
        if self.timer is not None:
            self.node.destroy_timer(self.timer)
            self.timer = None
        for subscription in self.subscriptions:
            self.node.destroy_subscription(subscription)
        self.subscriptions.clear()
        for csv_file in self.frame_timestamp_files.values():
            csv_file.flush()
            csv_file.close()
        self.frame_timestamp_files.clear()
        self.frame_timestamp_writers.clear()
        self.frame_timestamp_rows_since_flush.clear()
        self.csv_file.close()
