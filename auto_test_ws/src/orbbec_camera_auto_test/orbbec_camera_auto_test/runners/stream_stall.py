from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .functional import _parse_launch_args, _require_detected_camera
from .performance import _wait_for_camera_ready
from .restart import _parse_duration_value
from ..core.reporter import ensure_dir, write_json, write_markdown
from ..core.ros_utils import RosHarness, resolve_message_type
from ..core.session import TestSession
from ..profile.templating import expand_camera_template


CSV_HEADER = [
    "frame_index",
    "topic",
    "header_seq",
    "message_stamp",
    "message_stamp_interval_sec",
    "receive_monotonic",
    "receive_monotonic_interval_sec",
]


def _make_status_logger(*log_paths: Path):
    def emit(message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        for log_path in log_paths:
            write_path = Path(log_path)
            write_path.parent.mkdir(parents=True, exist_ok=True)
            with write_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    return emit


def _safe_csv_name(topic: str) -> str:
    name = topic.strip("/")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return f"{name or 'root'}.csv"


def _stamp_to_seconds(stamp: Any) -> float:
    if hasattr(stamp, "to_sec"):
        return float(stamp.to_sec())
    sec = getattr(stamp, "sec", getattr(stamp, "secs", 0))
    nanosec = getattr(stamp, "nanosec", getattr(stamp, "nsecs", 0))
    return float(sec) + float(nanosec) * 1e-9


def _message_stamp(message: Any) -> float:
    header = getattr(message, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return 0.0
    return _stamp_to_seconds(stamp)


def _header_seq(message: Any) -> str:
    header = getattr(message, "header", None)
    value = getattr(header, "seq", "")
    return "" if value is None else str(value)


def _format_optional(value: Any) -> str:
    return "" if value == "" or value is None else f"{float(value):.9f}"


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _image_topics(raw_topics: List[str], camera_name: str) -> List[str]:
    topics = [item.strip() for item in raw_topics if item.strip()]
    if not topics:
        topics = ["/{camera}/color/image_raw"]
    return [expand_camera_template(topic, camera_name) or topic for topic in topics]


def _build_launch_args(args) -> Dict[str, Any]:
    launch_args: Dict[str, Any] = {}
    if args.camera_name:
        launch_args["camera_name"] = args.camera_name
    if args.serial_number:
        launch_args["serial_number"] = args.serial_number
    if args.usb_port:
        launch_args["usb_port"] = args.usb_port
    if args.config_file_path:
        launch_args["config_file_path"] = args.config_file_path
    launch_args.update(_parse_launch_args(args.launch_arg))
    return launch_args


class WarningLogWriter:
    def __init__(self, log_file: Path) -> None:
        self.log_file = log_file
        self.lock = threading.Lock()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.log_file.open("w", encoding="utf-8")

    def write(self, message: str) -> None:
        line = f"[{datetime.now().isoformat()}] {message}\n"
        with self.lock:
            if not self.stream.closed:
                self.stream.write(line)
                self.stream.flush()

    def close(self) -> None:
        with self.lock:
            if not self.stream.closed:
                self.stream.flush()
                self.stream.close()


class TopicReceiveLogger:
    def __init__(
        self,
        topic: str,
        csv_file: Path,
        warning_interval_sec: float,
        warmup_end_monotonic: float,
        warning_log_writer: WarningLogWriter,
        save_csv: bool,
    ) -> None:
        self.topic = topic
        self.csv_file = csv_file
        self.warning_interval_sec = warning_interval_sec
        self.warmup_end_monotonic = warmup_end_monotonic
        self.warning_log_writer = warning_log_writer
        self.save_csv = save_csv
        self.lock = threading.Lock()
        self.frame_index = 0
        self.prev_msg_stamp = None
        self.prev_receive_monotonic = None
        self.warning_count = 0
        self.max_receive_interval_sec = 0.0
        self.message_count = 0
        self.closed = False
        self.csv_stream = None
        self.csv_writer = None
        if self.save_csv:
            self.csv_file.parent.mkdir(parents=True, exist_ok=True)
            self.csv_stream = self.csv_file.open("w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_stream)
            self.csv_writer.writerow(CSV_HEADER)
            self.csv_stream.flush()

    def write(self, message: Any) -> None:
        receive_monotonic = time.monotonic()
        msg_stamp = _message_stamp(message)
        with self.lock:
            if self.closed:
                return
            msg_interval = ""
            recv_interval = ""
            if self.prev_msg_stamp is not None:
                msg_interval = msg_stamp - self.prev_msg_stamp
            if self.prev_receive_monotonic is not None:
                recv_interval = receive_monotonic - self.prev_receive_monotonic
                self.max_receive_interval_sec = max(self.max_receive_interval_sec, recv_interval)
            self.prev_msg_stamp = msg_stamp
            self.prev_receive_monotonic = receive_monotonic

            if receive_monotonic < self.warmup_end_monotonic:
                return

            self.message_count += 1
            if self.save_csv and self.csv_writer is not None:
                self.frame_index += 1
                self.csv_writer.writerow(
                    [
                        self.frame_index,
                        self.topic,
                        _header_seq(message),
                        f"{msg_stamp:.9f}",
                        _format_optional(msg_interval),
                        f"{receive_monotonic:.9f}",
                        _format_optional(recv_interval),
                    ]
                )
                self.csv_stream.flush()

        if recv_interval != "" and recv_interval > self.warning_interval_sec:
            warning_message = (
                f"Image receive interval exceeded {self.warning_interval_sec:.3f}s: "
                f"topic={self.topic}, seq={_header_seq(message)}, "
                f"receive_monotonic_interval={recv_interval:.9f}s, "
                f"message_stamp_interval={_format_optional(msg_interval)}"
            )
            with self.lock:
                self.warning_count += 1
            self.warning_log_writer.write(warning_message)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "topic": self.topic,
                "message_count": self.message_count,
                "warning_count": self.warning_count,
                "max_receive_interval_sec": self.max_receive_interval_sec,
                "csv_file": str(self.csv_file) if self.save_csv else "",
            }

    def close(self) -> None:
        with self.lock:
            self.closed = True
            if self.csv_stream is not None and not self.csv_stream.closed:
                self.csv_stream.flush()
                self.csv_stream.close()


class StreamStallCollector:
    def __init__(
        self,
        harness: RosHarness,
        topics: List[str],
        output_dir: Path,
        warning_interval_sec: float,
        warmup_sec: float,
        save_csv: bool,
        queue_size: int,
    ) -> None:
        self.harness = harness
        self.topics = topics
        self.output_dir = ensure_dir(output_dir)
        self.warning_log_writer = WarningLogWriter(self.output_dir / "warnings.log")
        self.loggers: Dict[str, TopicReceiveLogger] = {}
        self.subscriptions = []
        warmup_end = time.monotonic() + warmup_sec
        image_type = resolve_message_type("sensor_msgs/msg/Image", harness.ros_version)
        for topic in topics:
            logger = TopicReceiveLogger(
                topic,
                self.output_dir / _safe_csv_name(topic),
                warning_interval_sec,
                warmup_end,
                self.warning_log_writer,
                save_csv,
            )
            self.loggers[topic] = logger
            self.subscriptions.append(
                harness.node.create_subscription(
                    image_type,
                    topic,
                    lambda msg, topic_name=topic: self.loggers[topic_name].write(msg),
                    queue_size,
                )
            )

    def summary(self) -> Dict[str, Dict[str, Any]]:
        return {topic: logger.snapshot() for topic, logger in self.loggers.items()}

    def close(self) -> None:
        for subscription in self.subscriptions:
            self.harness.node.destroy_subscription(subscription)
        self.subscriptions = []
        for logger in self.loggers.values():
            logger.close()
        self.warning_log_writer.close()


def _build_summary(result: Dict[str, Any]) -> str:
    lines = [
        f"# Stream Stall Test Summary: {result.get('launch_file', '')}",
        "",
        "## Overview",
        "",
        f"- Status: {result.get('status', '')}",
        f"- Duration seconds: {result.get('duration_seconds', 0)}",
        f"- Warning interval seconds: {result.get('warning_interval_sec', 0)}",
        f"- Warmup seconds: {result.get('warmup_sec', 0)}",
        "",
        "## Topics",
        "",
        "| Topic | Messages | Warnings | Max receive interval sec | CSV |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in result.get("topics", {}).values():
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("topic", "")),
                    str(item.get("message_count", 0)),
                    str(item.get("warning_count", 0)),
                    f"{float(item.get('max_receive_interval_sec', 0.0) or 0.0):.6f}",
                    str(item.get("csv_file", "")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def run_stream_stall_test(args) -> int:
    results_dir = ensure_dir(Path(args.results_dir).resolve())
    launch_log_path = results_dir / "launch.log"
    stream_log_path = results_dir / "stream_stall.log"
    emit_status = _make_status_logger(stream_log_path)
    duration_seconds = _parse_duration_value(args.duration, 3600.0)
    warning_interval_sec = float(args.warning_interval_sec)
    warmup_sec = float(args.warmup_sec)
    save_csv = _parse_bool(args.save_csv)
    queue_size = max(int(args.queue_size), 1)
    launch_args = _build_launch_args(args)
    camera_name = str(launch_args.get("camera_name", "camera"))
    topics = _image_topics(args.image_topic or [], camera_name)

    result: Dict[str, Any] = {
        "status": "passed",
        "ros_version": str(args.ros_version),
        "launch_file": args.launch_file,
        "launch_args": launch_args,
        "camera_name": camera_name,
        "duration_seconds": duration_seconds,
        "warning_interval_sec": warning_interval_sec,
        "warmup_sec": warmup_sec,
        "save_csv": save_csv,
        "queue_size": queue_size,
        "topics": {},
    }
    write_json(results_dir / "launch_args.json", {"launch_file": args.launch_file, "launch_args": launch_args})

    try:
        _require_detected_camera(
            args.driver_setup,
            emit_status,
            ros_version=args.ros_version,
            ros_setup=args.ros_setup,
        )
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = str(exc)
        emit_status(f"stream stall preflight failed: {exc}")
        write_json(results_dir / "result.json", result)
        write_markdown(results_dir / "summary.md", _build_summary(result))
        return 1

    session = TestSession(
        launch_file=args.launch_file,
        launch_args=launch_args,
        work_dir=results_dir,
        log_path=launch_log_path,
        driver_setup=args.driver_setup,
        ros_version=args.ros_version,
        ros_setup=args.ros_setup,
        status_callback=emit_status,
    )
    collector = None
    harness_context = None
    try:
        emit_status(f"starting stream stall launch: {args.launch_file}")
        session.start()
        harness_context = RosHarness("orbbec_camera_stream_stall_test", ros_version=args.ros_version)
        harness = harness_context.__enter__()
        _wait_for_camera_ready(session, harness, camera_name, emit_status)
        for topic in topics:
            emit_status(f"[STREAM_STALL] monitoring {topic}")
        collector = StreamStallCollector(
            harness,
            topics,
            results_dir / "image_receive_stats",
            warning_interval_sec,
            warmup_sec,
            save_csv,
            queue_size,
        )
        deadline = time.monotonic() + duration_seconds
        next_progress = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            harness.spin_once(0.1)
            session.assert_running()
            if time.monotonic() >= next_progress:
                result["topics"] = collector.summary()
                counts = ", ".join(
                    f"{item['topic']} messages={item['message_count']} warnings={item['warning_count']}"
                    for item in result["topics"].values()
                )
                emit_status(f"[STREAM_STALL] {counts}")
                write_json(results_dir / "result.json", result)
                next_progress = time.monotonic() + 10.0
        result["topics"] = collector.summary()
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["error"] = "interrupted by user"
        emit_status("stream stall test interrupted by user")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = str(exc)
        emit_status(f"stream stall test failed: {exc}")
    finally:
        if collector is not None:
            result["topics"] = collector.summary()
            collector.close()
        if harness_context is not None:
            harness_context.__exit__(None, None, None)
        session.stop()

    write_json(results_dir / "result.json", result)
    write_markdown(results_dir / "summary.md", _build_summary(result))
    return 0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1


def parse_args():
    parser = argparse.ArgumentParser(description="Run long-duration image stream stall tests")
    parser.add_argument("--launch-file", required=True)
    parser.add_argument("--camera-name", default=None)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--usb-port", default=None)
    parser.add_argument("--config-file-path", default=None)
    parser.add_argument("--driver-setup", default=None)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--duration", default="1h")
    parser.add_argument("--warning-interval-sec", default="1.0")
    parser.add_argument("--warmup-sec", default="2.0")
    parser.add_argument("--save-csv", default="true")
    parser.add_argument("--queue-size", default="10")
    parser.add_argument("--image-topic", action="append", default=[])
    parser.add_argument("--launch-arg", action="append", default=[])
    parser.add_argument(
        "--ros-version",
        choices=("1", "2"),
        default=os.environ.get("ORBBEC_ROS_VERSION", "2"),
    )
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sys.exit(run_stream_stall_test(args))


if __name__ == "__main__":
    main()
