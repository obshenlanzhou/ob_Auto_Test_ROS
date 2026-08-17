#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Subscribe multiple ROS/ROS2 image topics and write per-topic receive statistics.

The statistics, CSV writing, warning logic, and metadata output are shared
between ROS1 and ROS2. Runtime-specific code is kept in thin backend classes.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

from _test_protocol import (
    EventWriter,
    artifact_list,
    atomic_write_json,
    collect_test_environment,
    contract_result,
    install_terminal_log,
    iso_now,
    namespace_request,
    test_environment_markdown,
)

DEFAULT_WARNING_INTERVAL_SEC = 1.0
DEFAULT_QUEUE_SIZE = 10
DEFAULT_BUFF_SIZE_MB = 16
DEFAULT_WARMUP_SEC = 2.0
DEFAULT_SAVE_CSV = True
DEFAULT_QOS = "sensor_data"
TOOL_VERSION = "1.9"
TEST_ID = "image_receive_stats_test"
ENV_READY_VAR = "IMAGE_RECEIVE_STATS_ENV_READY"
SUMMARY_UPDATE_INTERVAL_SEC = 10.0
MIN_WARNING_CHECK_INTERVAL_SEC = 0.05
MAX_WARNING_CHECK_INTERVAL_SEC = 1.0
NANOSECONDS_PER_MICROSECOND = 1000
MICROSECONDS_PER_SECOND = 1000000


def csv_header(include_header_seq):
    header = [
        "frame_index",
        "topic",
    ]
    if include_header_seq:
        header.append("header_seq")
    header.extend(
        [
            "ros_header_stamp_sec",
            "ros_header_stamp_delta_sec",
            "receive_system_ts_sec",
            "receive_steady_ts_sec",
            "receive_steady_delta_sec",
        ]
    )
    return header


def summary_header(include_header_seq):
    header = [
        "topic",
        "delta_count",
        "receive_steady_delta_min_sec",
        "receive_steady_delta_max_sec",
        "receive_steady_delta_average_sec",
        "receive_steady_delta_warning_count",
        "no_frame_warning_count",
        (
            "max_receive_steady_delta_header_seq"
            if include_header_seq
            else "max_receive_steady_delta_frame_index"
        ),
        "max_receive_steady_delta_system_ts_sec",
        "ros_header_stamp_delta_min_sec",
        "ros_header_stamp_delta_max_sec",
        "ros_header_stamp_delta_average_sec",
        "ros_header_stamp_non_positive_delta_count",
    ]
    if include_header_seq:
        header.extend(
            [
                "header_seq_gap_count",
                "header_seq_backward_count",
                "header_seq_duplicate_count",
            ]
        )
    header.extend(["image_info_change_count", "zero_data_count"])
    return header


def parse_args(argv, ros_version):
    parser = argparse.ArgumentParser(
        description=(
            "Record per-frame {} image timestamps, host system receive timestamps, "
            "and steady-clock receive deltas to one CSV file per topic."
        ).format("ROS2" if ros_version == "ros2" else "ROS")
    )
    parser.add_argument("--ros-version", choices=("1", "2"), default=None)
    parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    parser.add_argument("--driver-setup", default=os.environ.get("ORBBEC_CAMERA_SETUP", ""))
    parser.add_argument("--results-dir", dest="output_dir", default=None, help="Results directory.")
    parser.add_argument(
        "--image-topic",
        dest="topics",
        action="append",
        default=None,
        help="sensor_msgs/Image topic; repeat for multiple topics.",
    )
    parser.add_argument(
        "--warning-interval-sec",
        dest="warning_interval_sec",
        type=float,
        default=None,
        help="Warn when consecutive receive deltas on a topic exceed this value.",
    )
    parser.add_argument(
        "--warmup-sec",
        dest="warmup_sec",
        type=float,
        default=None,
        help="Do not write CSV rows or warn during the first N seconds after startup.",
    )
    parser.add_argument(
        "--save-csv",
        dest="save_csv",
        default=None,
        choices=["true", "false", "1", "0", "yes", "no", "on", "off"],
        help="Enable or disable per-frame CSV saving; summary.csv is always enabled.",
    )
    parser.set_defaults(disable_csv=None)
    parser.add_argument("--queue-size", dest="queue_size", type=int, default=None, help="Subscriber queue size.")
    parser.add_argument(
        "--duration",
        default="",
        help="Optional maximum wall time, such as 300, 15m, or 2h",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Record a topic callback failure and keep monitoring other frames (default: stop)",
    )
    if ros_version == "ros1":
        parser.add_argument(
            "--buff-size",
            dest="buff_size",
            type=int,
            default=None,
            help="Subscriber socket buffer size in MB.",
        )
    else:
        parser.add_argument(
            "--qos",
            default=None,
            choices=["sensor_data", "default", "reliable", "best_effort"],
            help="Subscriber QoS profile.",
        )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(TOOL_VERSION),
    )
    return parser.parse_args(argv)


def parse_duration(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    multiplier = 1.0
    if text.endswith("s"):
        text = text[:-1]
    elif text.endswith("m"):
        text = text[:-1]
        multiplier = 60.0
    elif text.endswith("h"):
        text = text[:-1]
        multiplier = 3600.0
    duration = float(text) * multiplier
    if duration <= 0:
        raise ValueError("--duration must be > 0")
    return duration


def capture_sourced_env(ros_setup, driver_setup, ros_version):
    env = dict(os.environ)
    env["ROS_VERSION"] = ros_version
    commands = []
    for setup_file in (ros_setup, driver_setup):
        setup_file = str(setup_file or "").strip()
        if not setup_file:
            continue
        setup_path = Path(setup_file).expanduser()
        if not setup_path.is_file():
            raise FileNotFoundError("setup file not found: {}".format(setup_path))
        commands.append("source {} >/dev/null 2>&1".format(shlex.quote(str(setup_path))))
    if not commands:
        return env
    raw = subprocess.check_output(
        ["bash", "-lc", " && ".join(commands) + " && env -0"],
        env=env,
    )
    sourced = {}
    for chunk in raw.split(b"\0"):
        if chunk and b"=" in chunk:
            key, value = chunk.split(b"=", 1)
            sourced[key.decode("utf-8")] = value.decode("utf-8")
    sourced["ROS_VERSION"] = ros_version
    sourced["PYTHONUNBUFFERED"] = "1"
    return sourced


def prepare_runtime_env(args, ros_version):
    if os.environ.get(ENV_READY_VAR) == "1":
        return
    runtime_env = capture_sourced_env(args.ros_setup, args.driver_setup, ros_version)
    runtime_env[ENV_READY_VAR] = "1"
    if args.ros_setup or args.driver_setup:
        executable = sys.executable or "python3"
        os.execvpe(executable, [executable, *sys.argv], runtime_env)


def parse_topics(value):
    if isinstance(value, (list, tuple)):
        return [str(topic).strip() for topic in value if str(topic).strip()]
    if not value:
        return []
    normalized = str(value).strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return [topic.strip().strip("'\"") for topic in normalized.split(",") if topic.strip()]


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes", "y", "on"):
        return True
    if normalized in ("false", "0", "no", "n", "off"):
        return False
    raise ValueError("Invalid boolean value: {}".format(value))


def default_output_dir(ros_version):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if ros_version == "ros2":
        return os.path.abspath("image_receive_stats_ros2_{}".format(timestamp))
    return os.path.abspath("image_receive_stats_{}".format(timestamp))


def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)


def safe_csv_name(topic):
    name = topic.strip("/")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    if not name:
        name = "root"
    return "{}.csv".format(name)


def ns_to_us(ns_value):
    if ns_value < 0:
        return -((-ns_value) // NANOSECONDS_PER_MICROSECOND)
    return ns_value // NANOSECONDS_PER_MICROSECOND


def seconds_to_us(seconds):
    return int(round(seconds * MICROSECONDS_PER_SECOND))


def steady_time_us():
    if hasattr(time, "monotonic_ns"):
        return ns_to_us(time.monotonic_ns())
    return seconds_to_us(time.monotonic())


def system_time_us():
    if hasattr(time, "time_ns"):
        return ns_to_us(time.time_ns())
    return seconds_to_us(time.time())


def format_us_as_sec(us_value):
    sign = "-" if us_value < 0 else ""
    absolute_us = abs(us_value)
    return "{}{}.{:06d}".format(
        sign,
        absolute_us // MICROSECONDS_PER_SECOND,
        absolute_us % MICROSECONDS_PER_SECOND,
    )


def format_optional_us_as_sec(value):
    if value is None or value == "":
        return ""
    return format_us_as_sec(value)


def rounded_average_us(total_us, count):
    if count <= 0:
        return None
    sign = -1 if total_us < 0 else 1
    return sign * ((abs(total_us) + count // 2) // count)


def warning_check_interval_sec(warning_interval_sec):
    return min(
        MAX_WARNING_CHECK_INTERVAL_SEC,
        max(MIN_WARNING_CHECK_INTERVAL_SEC, warning_interval_sec / 2.0),
    )


def write_csv_atomic(csv_file, header, rows):
    temporary_file = csv_file + ".tmp"
    try:
        with open(temporary_file, "w", newline="") as csv_fh:
            csv_writer = csv.writer(csv_fh)
            csv_writer.writerow(header)
            csv_writer.writerows(rows)
            csv_fh.flush()
            os.fsync(csv_fh.fileno())
        os.replace(temporary_file, csv_file)
    except Exception:
        if os.path.exists(temporary_file):
            try:
                os.remove(temporary_file)
            except OSError:
                pass
        raise


def write_json_atomic(json_file, data):
    temporary_file = json_file + ".tmp"
    try:
        with open(temporary_file, "w") as json_fh:
            json.dump(data, json_fh, indent=2, sort_keys=True)
            json_fh.write("\n")
            json_fh.flush()
            os.fsync(json_fh.fileno())
        os.replace(temporary_file, json_file)
    except Exception:
        if os.path.exists(temporary_file):
            try:
                os.remove(temporary_file)
            except OSError:
                pass
        raise


class WarningLogWriter:
    def __init__(self, log_file):
        self.log_file = log_file
        self.lock = threading.Lock()
        self.log_fh = open(self.log_file, "w")

    def write(self, message):
        wall_time = datetime.now().isoformat()
        line = "[{}] {}\n".format(wall_time, message)
        with self.lock:
            if not self.log_fh.closed:
                self.log_fh.write(line)
                self.log_fh.flush()

    def close(self):
        with self.lock:
            if not self.log_fh.closed:
                self.log_fh.flush()
                self.log_fh.close()


class TopicCsvLogger:
    def __init__(
        self,
        topic,
        csv_file,
        warning_interval_sec,
        warmup_end_steady_us,
        warning_log_writer,
        save_csv,
        include_header_seq,
    ):
        self.topic = topic
        self.csv_file = csv_file
        self.warning_interval_sec = warning_interval_sec
        self.warning_interval_us = seconds_to_us(warning_interval_sec)
        self.warmup_end_steady_us = warmup_end_steady_us
        self.warning_log_writer = warning_log_writer
        self.save_csv = save_csv
        self.include_header_seq = include_header_seq
        self.lock = threading.Lock()
        self.frame_index = 0
        self.prev_ros_header_stamp_us = None
        self.prev_receive_steady_us = None
        self.prev_receive_system_us = None
        self.prev_header_seq = None
        self.no_frame_warning_active = False
        self.no_frame_warning_count = 0
        self.delta_count = 0
        self.receive_steady_delta_min_us = None
        self.receive_steady_delta_max_us = None
        self.receive_steady_delta_total_us = 0
        self.receive_steady_delta_warning_count = 0
        self.max_receive_steady_delta_frame_ref = None
        self.max_receive_steady_delta_system_us = None
        self.ros_header_stamp_delta_min_us = None
        self.ros_header_stamp_delta_max_us = None
        self.ros_header_stamp_positive_delta_count = 0
        self.ros_header_stamp_positive_delta_total_us = 0
        self.ros_header_stamp_non_positive_delta_count = 0
        self.header_seq_gap_count = 0
        self.header_seq_backward_count = 0
        self.header_seq_duplicate_count = 0
        self.prev_image_info = None
        self.image_info_change_count = 0
        self.zero_data_count = 0
        self.closed = False

        self.csv_fh = None
        self.csv_writer = None
        if self.save_csv:
            self.csv_fh = open(self.csv_file, "w", newline="")
            self.csv_writer = csv.writer(self.csv_fh)
            self.csv_writer.writerow(csv_header(self.include_header_seq))
            self.csv_fh.flush()

    def _record_deltas(
        self,
        ros_header_stamp_delta_us,
        receive_steady_delta_us,
        frame_ref,
        receive_system_us,
    ):
        self.delta_count += 1

        if (
            self.receive_steady_delta_min_us is None
            or receive_steady_delta_us < self.receive_steady_delta_min_us
        ):
            self.receive_steady_delta_min_us = receive_steady_delta_us
        if (
            self.receive_steady_delta_max_us is None
            or receive_steady_delta_us > self.receive_steady_delta_max_us
        ):
            self.receive_steady_delta_max_us = receive_steady_delta_us
            self.max_receive_steady_delta_frame_ref = frame_ref
            self.max_receive_steady_delta_system_us = receive_system_us
        self.receive_steady_delta_total_us += receive_steady_delta_us

        if receive_steady_delta_us > self.warning_interval_us:
            self.receive_steady_delta_warning_count += 1

        if (
            self.ros_header_stamp_delta_min_us is None
            or ros_header_stamp_delta_us < self.ros_header_stamp_delta_min_us
        ):
            self.ros_header_stamp_delta_min_us = ros_header_stamp_delta_us
        if (
            self.ros_header_stamp_delta_max_us is None
            or ros_header_stamp_delta_us > self.ros_header_stamp_delta_max_us
        ):
            self.ros_header_stamp_delta_max_us = ros_header_stamp_delta_us
        if ros_header_stamp_delta_us > 0:
            self.ros_header_stamp_positive_delta_count += 1
            self.ros_header_stamp_positive_delta_total_us += ros_header_stamp_delta_us
        else:
            self.ros_header_stamp_non_positive_delta_count += 1

    def _record_header_seq(self, header_seq):
        if not self.include_header_seq or header_seq is None:
            return
        if self.prev_header_seq is None:
            return
        if header_seq > self.prev_header_seq + 1:
            self.header_seq_gap_count += 1
        elif header_seq < self.prev_header_seq:
            self.header_seq_backward_count += 1
        elif header_seq == self.prev_header_seq:
            self.header_seq_duplicate_count += 1

    def _record_image_info(self, msg):
        image_info = (msg.width, msg.height, msg.encoding, msg.step, len(msg.data))
        if self.prev_image_info is not None and image_info != self.prev_image_info:
            self.image_info_change_count += 1
        self.prev_image_info = image_info
        if len(msg.data) == 0:
            self.zero_data_count += 1

    def write(self, msg, ros_header_stamp_us, header_seq, warn):
        receive_system_us = system_time_us()
        receive_steady_us = steady_time_us()

        with self.lock:
            if self.closed:
                return

            if receive_steady_us < self.warmup_end_steady_us:
                self.prev_ros_header_stamp_us = None
                self.prev_receive_steady_us = None
                self.prev_receive_system_us = None
                self.prev_header_seq = None
                self.prev_image_info = None
                self.no_frame_warning_active = False
                return

            self.frame_index += 1
            frame_index = self.frame_index
            frame_ref = header_seq if self.include_header_seq else frame_index

            ros_header_stamp_delta_us = ""
            receive_steady_delta_us = ""
            if self.prev_ros_header_stamp_us is not None:
                ros_header_stamp_delta_us = (
                    ros_header_stamp_us - self.prev_ros_header_stamp_us
                )
            if self.prev_receive_steady_us is not None:
                receive_steady_delta_us = receive_steady_us - self.prev_receive_steady_us

            self.prev_ros_header_stamp_us = ros_header_stamp_us
            self.prev_receive_steady_us = receive_steady_us
            self.prev_receive_system_us = receive_system_us
            self.no_frame_warning_active = False

            should_warn = receive_steady_delta_us != "" and (
                receive_steady_delta_us > self.warning_interval_us
            )
            if receive_steady_delta_us != "":
                self._record_deltas(
                    ros_header_stamp_delta_us,
                    receive_steady_delta_us,
                    frame_ref,
                    receive_system_us,
                )
            self._record_header_seq(header_seq)
            self._record_image_info(msg)
            self.prev_header_seq = header_seq

            if self.save_csv and self.csv_fh is not None and not self.csv_fh.closed:
                row = [
                    frame_index,
                    self.topic,
                ]
                if self.include_header_seq:
                    row.append(header_seq)
                row.extend(
                    [
                        format_us_as_sec(ros_header_stamp_us),
                        format_optional_us_as_sec(ros_header_stamp_delta_us),
                        format_us_as_sec(receive_system_us),
                        format_us_as_sec(receive_steady_us),
                        format_optional_us_as_sec(receive_steady_delta_us),
                    ]
                )
                self.csv_writer.writerow(row)
                self.csv_fh.flush()

        if should_warn:
            frame_label = "seq" if self.include_header_seq else "frame_index"
            warning_message = (
                "Image receive delta exceeded {:.3f}s: topic={}, {}={}, "
                "receive_steady_delta_sec={}, ros_header_stamp_delta_sec={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                frame_label,
                frame_ref,
                format_us_as_sec(receive_steady_delta_us),
                format_optional_us_as_sec(ros_header_stamp_delta_us),
            )
            warn(warning_message)
            self.warning_log_writer.write(warning_message)

    def check_no_frame_warning(self, now_steady_us):
        with self.lock:
            if self.closed or now_steady_us < self.warmup_end_steady_us:
                return None

            if self.prev_receive_steady_us is None:
                no_frame_duration_us = now_steady_us - self.warmup_end_steady_us
            else:
                no_frame_duration_us = now_steady_us - self.prev_receive_steady_us

            if (
                no_frame_duration_us <= self.warning_interval_us
                or self.no_frame_warning_active
            ):
                return None

            self.no_frame_warning_active = True
            self.no_frame_warning_count += 1
            frame_label = "last_header_seq" if self.include_header_seq else "last_frame_index"
            frame_ref = self.prev_header_seq if self.include_header_seq else self.frame_index
            return (
                "Image topic no frame for more than {:.3f}s: topic={}, "
                "no_frame_duration_sec={}, {}={}, last_receive_system_ts_sec={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                format_us_as_sec(no_frame_duration_us),
                frame_label,
                frame_ref if frame_ref else "",
                format_optional_us_as_sec(self.prev_receive_system_us),
            )

    def summary_row(self):
        with self.lock:
            receive_steady_delta_average_us = rounded_average_us(
                self.receive_steady_delta_total_us,
                self.delta_count,
            )
            ros_header_stamp_delta_average_us = rounded_average_us(
                self.ros_header_stamp_positive_delta_total_us,
                self.ros_header_stamp_positive_delta_count,
            )
            row = [
                self.topic,
                self.delta_count,
                format_optional_us_as_sec(self.receive_steady_delta_min_us),
                format_optional_us_as_sec(self.receive_steady_delta_max_us),
                format_optional_us_as_sec(receive_steady_delta_average_us),
                self.receive_steady_delta_warning_count,
                self.no_frame_warning_count,
                (
                    self.max_receive_steady_delta_frame_ref
                    if self.max_receive_steady_delta_frame_ref is not None
                    else ""
                ),
                format_optional_us_as_sec(self.max_receive_steady_delta_system_us),
                format_optional_us_as_sec(self.ros_header_stamp_delta_min_us),
                format_optional_us_as_sec(self.ros_header_stamp_delta_max_us),
                format_optional_us_as_sec(ros_header_stamp_delta_average_us),
                self.ros_header_stamp_non_positive_delta_count,
            ]
            if self.include_header_seq:
                row.extend(
                    [
                        self.header_seq_gap_count,
                        self.header_seq_backward_count,
                        self.header_seq_duplicate_count,
                    ]
                )
            row.extend([self.image_info_change_count, self.zero_data_count])
            return row

    def close(self):
        with self.lock:
            self.closed = True
            if self.csv_fh is not None and not self.csv_fh.closed:
                self.csv_fh.flush()
                self.csv_fh.close()


class ReceiveStatsCore:
    def __init__(
        self,
        topics,
        output_dir,
        warning_interval_sec,
        queue_size,
        warmup_sec,
        save_csv,
        include_header_seq,
        metadata,
        request,
    ):
        if not topics:
            raise ValueError("No image topics configured.")
        if warning_interval_sec <= 0:
            raise ValueError("warning_interval_sec must be greater than 0.")
        if queue_size == 0:
            raise ValueError("queue_size must be greater than 0, or -1 for unlimited.")
        if warmup_sec < 0:
            raise ValueError("warmup_sec must be greater than or equal to 0.")

        self.topics = topics
        self.output_dir = output_dir
        self.warning_interval_sec = warning_interval_sec
        self.warning_check_interval_sec = warning_check_interval_sec(warning_interval_sec)
        self.queue_size = queue_size
        self.warmup_sec = warmup_sec
        self.save_csv = save_csv
        self.include_header_seq = include_header_seq
        self.metadata = metadata
        self.request = request
        self.continue_on_failure = bool(request.get("continue_on_failure", False))
        self.environment = collect_test_environment(request)
        self.run_id = os.path.basename(os.path.normpath(output_dir))
        self.started_at = iso_now()
        self.started_monotonic = time.monotonic()
        self.status = "passed"
        self.loggers = {}
        self.warning_log_writer = None
        self.summary_file = os.path.join(self.output_dir, "summary.csv")
        self.metadata_file = os.path.join(self.output_dir, "metadata.json")
        self.summary_update_lock = threading.Lock()
        self.callback_condition = threading.Condition()
        self.active_callback_count = 0
        self.close_lock = threading.Lock()
        self.closed = False

        ensure_dir(self.output_dir)
        install_terminal_log(Path(self.output_dir) / "terminal.log")
        self.events = EventWriter(Path(self.output_dir) / "events.jsonl")
        self.events.emit("phase", "image receive statistics started", phase="running")
        self.warning_log_writer = WarningLogWriter(os.path.join(self.output_dir, "warnings.log"))
        warmup_end_steady_us = steady_time_us() + seconds_to_us(self.warmup_sec)

        used_csv_files = {self.summary_file}
        for topic in self.topics:
            csv_file = os.path.join(self.output_dir, safe_csv_name(topic))
            if self.save_csv:
                if csv_file in used_csv_files:
                    raise ValueError("Conflicting CSV output path for topic: {}".format(topic))
                used_csv_files.add(csv_file)
            self.loggers[topic] = TopicCsvLogger(
                topic,
                csv_file,
                self.warning_interval_sec,
                warmup_end_steady_us,
                self.warning_log_writer,
                self.save_csv,
                self.include_header_seq,
            )

        self.write_metadata()
        self.write_summary()

    def record_failure(self, message):
        self.status = "failed"
        self.events.emit("failure", message, status="failed")

    def write_metadata(self):
        data = {
            "started_at": datetime.now().isoformat(),
            "tool_version": TOOL_VERSION,
            "topics": self.topics,
            "output_dir": self.output_dir,
            "warning_interval_sec": self.warning_interval_sec,
            "warning_check_interval_sec": self.warning_check_interval_sec,
            "queue_size": self.queue_size,
            "warmup_sec": self.warmup_sec,
            "save_csv": self.save_csv,
            "continue_on_failure": self.continue_on_failure,
            "ros_distro": os.environ.get("ROS_DISTRO", ""),
        }
        data.update(self.metadata)
        write_json_atomic(self.metadata_file, data)

    def begin_callback(self):
        with self.callback_condition:
            if self.closed:
                return False
            self.active_callback_count += 1
            return True

    def end_callback(self):
        with self.callback_condition:
            self.active_callback_count -= 1
            if self.active_callback_count == 0:
                self.callback_condition.notify_all()

    def collect_summary_rows(self):
        return [self.loggers[topic].summary_row() for topic in self.topics]

    def write_summary(self):
        with self.summary_update_lock:
            rows = self.collect_summary_rows()
            write_csv_atomic(self.summary_file, summary_header(self.include_header_seq), rows)
            return rows

    def summary_timer_callback(self, log_error):
        with self.summary_update_lock:
            if self.closed:
                return
            try:
                rows = self.collect_summary_rows()
                write_csv_atomic(self.summary_file, summary_header(self.include_header_seq), rows)
            except Exception as exc:
                log_error(
                    "Failed to update summary CSV {}: {}".format(
                        self.summary_file,
                        exc,
                    )
                )

    def warning_timer_callback(self, warn):
        now_steady_us = steady_time_us()
        for topic in self.topics:
            with self.callback_condition:
                if self.closed:
                    return
            warning_message = self.loggers[topic].check_no_frame_warning(now_steady_us)
            if warning_message:
                warn(warning_message)
                self.warning_log_writer.write(warning_message)
                self.events.emit("warning", warning_message, code="no-frame")

    def log_summary(self, rows, log_info):
        header = summary_header(self.include_header_seq)
        for row in rows:
            summary = dict(zip(header, row))
            message = (
                "Image receive summary: topic={}, deltas={}, "
                "receive_steady_delta_sec[min/max/average]={}/{}/{}, delta_warnings={}, "
                "no_frame_warnings={}, "
                "ros_header_stamp_delta_sec[min/max/average]={}/{}/{}, non_positive={}, "
            ).format(
                summary["topic"],
                summary["delta_count"],
                summary["receive_steady_delta_min_sec"],
                summary["receive_steady_delta_max_sec"],
                summary["receive_steady_delta_average_sec"],
                summary["receive_steady_delta_warning_count"],
                summary["no_frame_warning_count"],
                summary["ros_header_stamp_delta_min_sec"],
                summary["ros_header_stamp_delta_max_sec"],
                summary["ros_header_stamp_delta_average_sec"],
                summary["ros_header_stamp_non_positive_delta_count"],
            )
            if self.include_header_seq:
                message += "header_seq[gap/backward/duplicate]={}/{}/{}, ".format(
                    summary["header_seq_gap_count"],
                    summary["header_seq_backward_count"],
                    summary["header_seq_duplicate_count"],
                )
            message += "image_info_changes={}, zero_data={}".format(
                summary["image_info_change_count"],
                summary["zero_data_count"],
            )
            log_info(message)

    def close(
        self,
        cancel_timers,
        log_error,
        log_info,
        should_log_summary=True,
        before_final_summary=None,
    ):
        with self.close_lock:
            with self.callback_condition:
                if self.closed:
                    return
                self.closed = True

            cancel_timers()

            with self.callback_condition:
                while self.active_callback_count > 0:
                    self.callback_condition.wait()

            if before_final_summary is not None:
                try:
                    before_final_summary()
                except Exception as exc:
                    log_error(
                        "Failed to complete pending summary CSV update {}: {}".format(
                            self.summary_file,
                            exc,
                        )
                    )

            with self.summary_update_lock:
                rows = self.collect_summary_rows()
                try:
                    write_csv_atomic(
                        self.summary_file,
                        summary_header(self.include_header_seq),
                        rows,
                    )
                except Exception as exc:
                    log_error(
                        "Failed to write final summary CSV {}: {}".format(
                            self.summary_file,
                            exc,
                        )
                    )
                if should_log_summary:
                    self.log_summary(rows, log_info)

            for logger in self.loggers.values():
                logger.close()
            if self.warning_log_writer is not None:
                self.warning_log_writer.close()

            header = summary_header(self.include_header_seq)
            summary_rows = [dict(zip(header, row)) for row in rows]
            warning_count = sum(
                int(row.get("receive_steady_delta_warning_count", 0) or 0)
                + int(row.get("no_frame_warning_count", 0) or 0)
                for row in summary_rows
            )
            elapsed_seconds = time.monotonic() - self.started_monotonic
            details = {
                "status": self.status,
                "tool_version": TOOL_VERSION,
                "environment": self.environment,
                "topics": self.topics,
                "continue_on_failure": self.continue_on_failure,
                "elapsed_seconds": elapsed_seconds,
                "topic_summaries": summary_rows,
                "warnings": (
                    [{"code": "receive-warning", "count": warning_count}]
                    if warning_count
                    else []
                ),
            }
            summary = {
                "topic_count": len(self.topics),
                "warning_count": warning_count,
            }
            summary_lines = [
                "# Image Receive Statistics",
                "",
                *test_environment_markdown(self.environment),
                "## Result",
                "",
                "- Status: {}".format(self.status),
                "- Topics: {}".format(len(self.topics)),
                "- Warnings: {}".format(warning_count),
                "- Elapsed seconds: {:.1f}".format(elapsed_seconds),
                "",
                "See `summary.csv` for per-topic metrics.",
                "",
            ]
            Path(self.output_dir, "summary.md").write_text(
                "\n".join(summary_lines),
                encoding="utf-8",
            )
            self.events.emit(
                "completed",
                "image receive statistics finished",
                status=self.status,
            )
            payload = contract_result(
                test_id=TEST_ID,
                run_id=self.run_id,
                started_at=self.started_at,
                ended_at=iso_now(),
                request=self.request,
                details=details,
                summary=summary,
                artifacts=artifact_list(Path(self.output_dir)),
            )
            atomic_write_json(Path(self.output_dir) / "result.json", payload)


def validate_unique_topics(topics):
    duplicate_topics = sorted({topic for topic in topics if topics.count(topic) > 1})
    if duplicate_topics:
        raise ValueError(
            "Duplicate image topics after ROS name resolution: {}".format(
                ", ".join(duplicate_topics)
            )
        )


def run_ros1():
    import rospy
    from sensor_msgs.msg import Image

    class Ros1ReceiveStatsNode:
        def __init__(
            self,
            topics,
            output_dir,
            warning_interval_sec,
            queue_size,
            buff_size_mb,
            warmup_sec,
            save_csv,
            request,
        ):
            if buff_size_mb <= 0:
                raise ValueError("buff_size must be greater than 0 MB.")

            self.topics = [rospy.resolve_name(topic) for topic in topics]
            validate_unique_topics(self.topics)
            self.buff_size_mb = buff_size_mb
            self.buff_size_bytes = buff_size_mb * 1024 * 1024
            self.subscribers = []
            self.summary_timer = None
            self.warning_timer = None
            self.core = ReceiveStatsCore(
                topics=self.topics,
                output_dir=output_dir,
                warning_interval_sec=warning_interval_sec,
                queue_size=queue_size,
                warmup_sec=warmup_sec,
                save_csv=save_csv,
                include_header_seq=True,
                metadata={
                    "buff_size_mb": self.buff_size_mb,
                    "buff_size_bytes": self.buff_size_bytes,
                },
                request=request,
            )

            for topic in self.topics:
                self.subscribers.append(
                    rospy.Subscriber(
                        topic,
                        Image,
                        self.image_callback,
                        callback_args=topic,
                        queue_size=queue_size,
                        buff_size=self.buff_size_bytes,
                    )
                )

            self.summary_timer = rospy.Timer(
                rospy.Duration.from_sec(SUMMARY_UPDATE_INTERVAL_SEC),
                self.summary_timer_callback,
            )
            self.warning_timer = rospy.Timer(
                rospy.Duration.from_sec(self.core.warning_check_interval_sec),
                self.warning_timer_callback,
            )
            rospy.on_shutdown(self.close)
            self.log_startup(queue_size, save_csv, warmup_sec)

        def log_info(self, message):
            rospy.loginfo("%s", message)

        def log_warn(self, message):
            rospy.logwarn("%s", message)

        def log_error(self, message):
            rospy.logerr("%s", message)

        def log_startup(self, queue_size, save_csv, warmup_sec):
            self.log_info("Tool version: {}".format(TOOL_VERSION))
            self.log_info(
                "Recording image receive stats to directory: {}".format(
                    self.core.output_dir
                )
            )
            self.log_info("Recording metadata to: {}".format(self.core.metadata_file))
            self.log_info(
                "Recording warning log to: {}".format(self.core.warning_log_writer.log_file)
            )
            self.log_info(
                "Recording cumulative summary every {:.1f}s to: {}".format(
                    SUMMARY_UPDATE_INTERVAL_SEC,
                    self.core.summary_file,
                )
            )
            self.log_info(
                "Checking no-frame warnings every {:.3f}s".format(
                    self.core.warning_check_interval_sec
                )
            )
            self.log_info(
                "Subscriber queue_size={} buff_size={}MB ({} bytes)".format(
                    queue_size,
                    self.buff_size_mb,
                    self.buff_size_bytes,
                )
            )
            self.log_info(
                "Per-frame CSV saving: {}".format("enabled" if save_csv else "disabled")
            )
            self.log_info("Warmup time before recording deltas: {:.3f}s".format(warmup_sec))
            for topic in self.topics:
                if save_csv:
                    self.log_info(
                        "Subscribing image topic: {} -> {}".format(
                            topic,
                            self.core.loggers[topic].csv_file,
                        )
                    )
                else:
                    self.log_info("Subscribing image topic: {}".format(topic))

        def image_callback(self, msg, topic):
            if not self.core.begin_callback():
                return
            try:
                try:
                    self.core.loggers[topic].write(
                        msg,
                        ns_to_us(msg.header.stamp.to_nsec()),
                        msg.header.seq,
                        self.log_warn,
                    )
                except Exception as exc:
                    message = "Image callback failed for {}: {}".format(topic, exc)
                    self.core.record_failure(message)
                    self.log_error(message)
                    if not self.core.continue_on_failure:
                        rospy.signal_shutdown(message)
            finally:
                self.core.end_callback()

        def summary_timer_callback(self, _event):
            self.core.summary_timer_callback(self.log_error)

        def warning_timer_callback(self, _event):
            self.core.warning_timer_callback(self.log_warn)

        def close(self):
            def cancel_timers():
                if self.summary_timer is not None:
                    self.summary_timer.shutdown()
                if self.warning_timer is not None:
                    self.warning_timer.shutdown()

            self.core.close(cancel_timers, self.log_error, self.log_info)

    def private_param_or_default(name, command_line_value, default_value):
        if command_line_value is not None:
            return command_line_value
        return rospy.get_param("~" + name, default_value)

    args = parse_args(rospy.myargv()[1:], "ros1")
    rospy.init_node("image_topic_receive_stats", anonymous=True)

    topics_param = private_param_or_default("topics", args.topics, "")
    configured_topics = parse_topics(topics_param)
    if not configured_topics:
        raise ValueError(
            "No image topics configured. Set --image-topic or the private ROS parameter ~topics."
        )

    save_csv = parse_bool(private_param_or_default("save_csv", args.save_csv, DEFAULT_SAVE_CSV))
    disable_csv = parse_bool(private_param_or_default("disable_csv", args.disable_csv, False))
    if disable_csv:
        save_csv = False

    node = Ros1ReceiveStatsNode(
        topics=configured_topics,
        output_dir=private_param_or_default(
            "output_dir",
            args.output_dir,
            default_output_dir("ros1"),
        ),
        warning_interval_sec=float(
            private_param_or_default(
                "warning_interval_sec",
                args.warning_interval_sec,
                DEFAULT_WARNING_INTERVAL_SEC,
            )
        ),
        queue_size=int(private_param_or_default("queue_size", args.queue_size, DEFAULT_QUEUE_SIZE)),
        buff_size_mb=int(
            private_param_or_default("buff_size", args.buff_size, DEFAULT_BUFF_SIZE_MB)
        ),
        warmup_sec=float(
            private_param_or_default("warmup_sec", args.warmup_sec, DEFAULT_WARMUP_SEC)
        ),
        save_csv=save_csv,
        request=namespace_request(args),
    )
    duration_seconds = parse_duration(args.duration)
    duration_timer = None
    if duration_seconds is not None:
        duration_timer = rospy.Timer(
            rospy.Duration.from_sec(duration_seconds),
            lambda _event: rospy.signal_shutdown("duration reached"),
            oneshot=True,
        )
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def request_stop(_signum, _frame):
        node.core.status = "interrupted"
        rospy.signal_shutdown("interrupted")

    signal.signal(signal.SIGINT, request_stop)
    try:
        rospy.spin()
    except KeyboardInterrupt:
        node.core.status = "interrupted"
    finally:
        if duration_timer is not None:
            duration_timer.shutdown()
        signal.signal(signal.SIGINT, previous_sigint_handler)
    node.close()
    if node.core.status == "interrupted":
        return 130
    return 0 if node.core.status == "passed" else 1


def run_ros2():
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
        qos_profile_sensor_data,
    )
    from rclpy.utilities import remove_ros_args
    from sensor_msgs.msg import Image

    def make_qos_profile(qos_name, queue_size):
        depth = queue_size if queue_size > 0 else 10
        if qos_name == "sensor_data":
            return qos_profile_sensor_data
        if qos_name == "reliable":
            return QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=depth,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            )
        if qos_name == "best_effort":
            return QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=depth,
                reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
            )
        return QoSProfile(depth=depth)

    def stamp_to_us(stamp):
        return seconds_to_us(stamp.sec) + ns_to_us(stamp.nanosec)

    def declare_parameters(node):
        node.declare_parameter("topics", "")
        node.declare_parameter("output_dir", "")
        node.declare_parameter("warning_interval_sec", DEFAULT_WARNING_INTERVAL_SEC)
        node.declare_parameter("warmup_sec", DEFAULT_WARMUP_SEC)
        node.declare_parameter("save_csv", DEFAULT_SAVE_CSV)
        node.declare_parameter("disable_csv", False)
        node.declare_parameter("queue_size", DEFAULT_QUEUE_SIZE)
        node.declare_parameter("qos", DEFAULT_QOS)

    def get_parameter_or_default(node, name, command_line_value, default_value):
        if command_line_value is not None:
            return command_line_value
        return node.get_parameter(name).value if node.has_parameter(name) else default_value

    class Ros2ReceiveStatsNode(Node):
        def __init__(
            self,
            topics,
            output_dir,
            warning_interval_sec,
            queue_size,
            qos,
            warmup_sec,
            save_csv,
            request,
        ):
            super().__init__("image_topic_receive_stats_ros2")

            self.topics = [self.resolve_topic_name(topic) for topic in topics]
            validate_unique_topics(self.topics)
            self.qos = qos
            self.qos_profile = make_qos_profile(qos, queue_size)
            self.subscribers = []
            self.summary_future_lock = threading.Lock()
            self.summary_future = None
            self.summary_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="image_receive_summary",
            )
            self.core = ReceiveStatsCore(
                topics=self.topics,
                output_dir=output_dir,
                warning_interval_sec=warning_interval_sec,
                queue_size=queue_size,
                warmup_sec=warmup_sec,
                save_csv=save_csv,
                include_header_seq=False,
                metadata={
                    "qos": self.qos,
                    "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION", ""),
                },
                request=request,
            )

            for topic in self.topics:
                self.subscribers.append(
                    self.create_subscription(
                        Image,
                        topic,
                        lambda msg, topic=topic: self.image_callback(msg, topic),
                        self.qos_profile,
                    )
                )

            self.summary_timer = self.create_timer(
                SUMMARY_UPDATE_INTERVAL_SEC,
                self.summary_timer_callback,
            )
            self.warning_timer = self.create_timer(
                self.core.warning_check_interval_sec,
                self.warning_timer_callback,
            )
            self.log_startup(queue_size, save_csv, warmup_sec)

        def log_info(self, message):
            self.get_logger().info(message)

        def log_warn(self, message):
            self.get_logger().warn(message)

        def log_error(self, message):
            self.get_logger().error(message)

        def log_startup(self, queue_size, save_csv, warmup_sec):
            self.log_info("Tool version: {}".format(TOOL_VERSION))
            self.log_info(
                "Recording image receive stats to directory: {}".format(
                    self.core.output_dir
                )
            )
            self.log_info("Recording metadata to: {}".format(self.core.metadata_file))
            self.log_info(
                "Recording warning log to: {}".format(self.core.warning_log_writer.log_file)
            )
            self.log_info(
                "Recording cumulative summary every {:.1f}s to: {}".format(
                    SUMMARY_UPDATE_INTERVAL_SEC,
                    self.core.summary_file,
                )
            )
            self.log_info(
                "Checking no-frame warnings every {:.3f}s".format(
                    self.core.warning_check_interval_sec
                )
            )
            self.log_info("Subscriber queue_size={} qos={}".format(queue_size, self.qos))
            self.log_info(
                "Per-frame CSV saving: {}".format("enabled" if save_csv else "disabled")
            )
            self.log_info("Warmup time before recording deltas: {:.3f}s".format(warmup_sec))
            for topic in self.topics:
                if save_csv:
                    self.log_info(
                        "Subscribing image topic: {} -> {}".format(
                            topic,
                            self.core.loggers[topic].csv_file,
                        )
                    )
                else:
                    self.log_info("Subscribing image topic: {}".format(topic))

        def image_callback(self, msg, topic):
            if not self.core.begin_callback():
                return
            try:
                try:
                    self.core.loggers[topic].write(
                        msg,
                        stamp_to_us(msg.header.stamp),
                        None,
                        self.log_warn,
                    )
                except Exception as exc:
                    message = "Image callback failed for {}: {}".format(topic, exc)
                    self.core.record_failure(message)
                    self.log_error(message)
                    if not self.core.continue_on_failure:
                        raise
            finally:
                self.core.end_callback()

        def summary_timer_callback(self):
            with self.summary_future_lock:
                if self.core.closed:
                    return
                if self.summary_future is not None:
                    if not self.summary_future.done():
                        return
                    try:
                        self.summary_future.result()
                    except Exception as exc:
                        self.log_error(
                            "Failed to update summary CSV {}: {}".format(
                                self.core.summary_file,
                                exc,
                            )
                        )
                self.summary_future = self.summary_executor.submit(self.core.write_summary)

        def wait_for_pending_summary_update(self):
            with self.summary_future_lock:
                summary_future = self.summary_future
                self.summary_future = None
            if summary_future is not None:
                summary_future.result()

        def warning_timer_callback(self):
            self.core.warning_timer_callback(self.log_warn)

        def close(self):
            def cancel_timers():
                self.summary_timer.cancel()
                self.warning_timer.cancel()

            self.core.close(
                cancel_timers,
                self.log_error,
                self.log_info,
                should_log_summary=rclpy.ok(),
                before_final_summary=self.wait_for_pending_summary_update,
            )
            self.summary_executor.shutdown(wait=True)

    rclpy.init()
    args = parse_args(remove_ros_args()[1:], "ros2")

    parameter_node = Node("image_topic_receive_stats_ros2_params")
    declare_parameters(parameter_node)
    node = None

    try:
        topics_param = get_parameter_or_default(parameter_node, "topics", args.topics, "")
        configured_topics = parse_topics(topics_param)
        if not configured_topics:
            raise ValueError(
                "No image topics configured. Set --image-topic or the ROS2 parameter topics."
            )

        save_csv = parse_bool(
            get_parameter_or_default(parameter_node, "save_csv", args.save_csv, DEFAULT_SAVE_CSV)
        )
        disable_csv = parse_bool(
            get_parameter_or_default(parameter_node, "disable_csv", args.disable_csv, False)
        )
        if disable_csv:
            save_csv = False

        output_dir = get_parameter_or_default(
            parameter_node,
            "output_dir",
            args.output_dir,
            "",
        )
        if not output_dir:
            output_dir = default_output_dir("ros2")

        node = Ros2ReceiveStatsNode(
            topics=configured_topics,
            output_dir=output_dir,
            warning_interval_sec=float(
                get_parameter_or_default(
                    parameter_node,
                    "warning_interval_sec",
                    args.warning_interval_sec,
                    DEFAULT_WARNING_INTERVAL_SEC,
                )
            ),
            queue_size=int(
                get_parameter_or_default(
                    parameter_node,
                    "queue_size",
                    args.queue_size,
                    DEFAULT_QUEUE_SIZE,
                )
            ),
            qos=str(get_parameter_or_default(parameter_node, "qos", args.qos, DEFAULT_QOS)),
            warmup_sec=float(
                get_parameter_or_default(
                    parameter_node,
                    "warmup_sec",
                    args.warmup_sec,
                    DEFAULT_WARMUP_SEC,
                )
            ),
            save_csv=save_csv,
            request=namespace_request(args),
        )
    finally:
        parameter_node.destroy_node()

    duration_seconds = parse_duration(args.duration)
    deadline = (
        time.monotonic() + duration_seconds if duration_seconds is not None else None
    )
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def request_stop(_signum, _frame):
        if node is not None:
            node.core.status = "interrupted"
        if rclpy.ok():
            rclpy.shutdown()

    signal.signal(signal.SIGINT, request_stop)
    try:
        try:
            while rclpy.ok():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                rclpy.spin_once(node, timeout_sec=0.2)
        except KeyboardInterrupt:
            if node is not None:
                node.core.status = "interrupted"
    finally:
        if node is not None:
            node.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        signal.signal(signal.SIGINT, previous_sigint_handler)
    if node is not None and node.core.status == "interrupted":
        return 130
    return 0 if node is None or node.core.status == "passed" else 1


def detect_ros_version():
    ros_version = os.environ.get("ROS_VERSION", "").strip()
    if ros_version == "1":
        return "ros1"
    if ros_version == "2":
        return "ros2"
    if "rclpy" in sys.modules:
        return "ros2"
    if "rospy" in sys.modules:
        return "ros1"
    try:
        import rclpy  # noqa: F401

        return "ros2"
    except ImportError:
        pass
    try:
        import rospy  # noqa: F401

        return "ros1"
    except ImportError:
        pass
    raise RuntimeError("Unable to detect ROS version. Source a ROS1 or ROS2 environment first.")


def main(ros_version=None):
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--ros-version",
        choices=("1", "2"),
        default=os.environ.get("ROS_VERSION", ""),
    )
    pre_parser.add_argument("--ros-setup", default=os.environ.get("ORBBEC_ROS_SETUP", ""))
    pre_parser.add_argument("--driver-setup", default=os.environ.get("ORBBEC_CAMERA_SETUP", ""))
    preliminary, _unknown = pre_parser.parse_known_args()
    requested_version = (
        ros_version
        or ({"1": "ros1", "2": "ros2"}.get(preliminary.ros_version))
    )
    resolved_ros_version = requested_version or detect_ros_version()
    if any(option in sys.argv[1:] for option in ("-h", "--help", "--version")):
        parse_args(sys.argv[1:], resolved_ros_version)
        return 0
    prepare_runtime_env(
        preliminary,
        "1" if resolved_ros_version == "ros1" else "2",
    )
    if resolved_ros_version == "ros1":
        return run_ros1()
    if resolved_ros_version == "ros2":
        return run_ros2()
    raise ValueError("Unsupported ROS version: {}".format(resolved_ros_version))


if __name__ == "__main__":
    sys.exit(main())
