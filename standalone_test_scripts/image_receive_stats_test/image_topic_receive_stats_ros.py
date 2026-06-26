#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Subscribe multiple ROS image topics and write per-topic receive statistics to CSV.

Image topics must be provided explicitly through --topics or the private ROS
parameter ~topics.

Each topic is written to its own CSV file. For every received frame the script
records the ROS header timestamp, the host system and steady receive clocks,
and the delta from the previous frame on the same topic. A compact
summary.csv is always updated periodically, even when per-frame CSV saving is
disabled.
"""

import argparse
import csv
from datetime import datetime
import json
import os
import re
import threading
import time

import rospy
from sensor_msgs.msg import Image


DEFAULT_WARNING_INTERVAL_SEC = 1.0
DEFAULT_QUEUE_SIZE = 10
DEFAULT_BUFF_SIZE_MB = 16
DEFAULT_WARMUP_SEC = 2.0
DEFAULT_SAVE_CSV = True
SUMMARY_UPDATE_INTERVAL_SEC = 10.0
MIN_WARNING_CHECK_INTERVAL_SEC = 0.05
MAX_WARNING_CHECK_INTERVAL_SEC = 1.0
NANOSECONDS_PER_MICROSECOND = 1000
MICROSECONDS_PER_SECOND = 1000000

CSV_HEADER = [
    "frame_index",
    "topic",
    "header_seq",
    "ros_header_stamp_sec",
    "ros_header_stamp_delta_sec",
    "receive_system_ts_sec",
    "receive_steady_ts_sec",
    "receive_steady_delta_sec",
]

SUMMARY_HEADER = [
    "topic",
    "delta_count",
    "receive_steady_delta_min_sec",
    "receive_steady_delta_max_sec",
    "receive_steady_delta_average_sec",
    "receive_steady_delta_warning_count",
    "no_frame_warning_count",
    "max_receive_steady_delta_header_seq",
    "max_receive_steady_delta_system_ts_sec",
    "ros_header_stamp_delta_min_sec",
    "ros_header_stamp_delta_max_sec",
    "ros_header_stamp_delta_average_sec",
    "ros_header_stamp_non_positive_delta_count",
    "header_seq_gap_count",
    "header_seq_backward_count",
    "header_seq_duplicate_count",
    "image_info_change_count",
    "zero_data_count",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Record per-frame ROS image timestamps, host system receive timestamps, "
            "and steady-clock receive deltas to one CSV file per topic."
        )
    )
    parser.add_argument("--output_dir", default=None, help="Directory for output CSV files.")
    parser.add_argument(
        "--topics",
        default=None,
        help="Required comma-separated sensor_msgs/Image topics. ROS param: _topics:='[...]'",
    )
    parser.add_argument(
        "--warning_interval_sec",
        type=float,
        default=None,
        help="Warn when consecutive receive deltas on a topic exceed this value.",
    )
    parser.add_argument(
        "--warmup_sec",
        type=float,
        default=None,
        help="Do not write CSV rows or warn during the first N seconds after startup.",
    )
    parser.add_argument(
        "--save_csv",
        default=None,
        choices=["true", "false", "1", "0", "yes", "no", "on", "off"],
        help=(
            "Enable or disable per-frame CSV saving; summary.csv is always enabled. "
            "ROS param: _save_csv:=false"
        ),
    )
    parser.add_argument(
        "--disable_csv",
        action="store_true",
        default=None,
        help="Disable per-frame CSV while keeping summary.csv and warning logs enabled.",
    )
    parser.add_argument("--queue_size", type=int, default=None, help="Subscriber queue size.")
    parser.add_argument(
        "--buff_size",
        type=int,
        default=None,
        help="Subscriber socket buffer size in MB.",
    )
    return parser.parse_args(rospy.myargv()[1:])


def private_param_or_default(name, command_line_value, default_value):
    if command_line_value is not None:
        return command_line_value
    return rospy.get_param("~" + name, default_value)


def parse_topics(value):
    if isinstance(value, (list, tuple)):
        return [str(topic).strip() for topic in value if str(topic).strip()]
    if not value:
        return []
    return [topic.strip() for topic in str(value).split(",") if topic.strip()]


def resolve_topics(topics):
    resolved_topics = [rospy.resolve_name(topic) for topic in topics]
    seen_topics = set()
    duplicate_topics = []
    for topic in resolved_topics:
        if topic in seen_topics and topic not in duplicate_topics:
            duplicate_topics.append(topic)
        seen_topics.add(topic)
    if duplicate_topics:
        raise ValueError(
            "Duplicate image topics after ROS name resolution: {}".format(
                ", ".join(duplicate_topics)
            )
        )
    return resolved_topics


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


def default_output_dir():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
    ):
        self.topic = topic
        self.csv_file = csv_file
        self.warning_interval_sec = warning_interval_sec
        self.warning_interval_us = seconds_to_us(warning_interval_sec)
        self.warmup_end_steady_us = warmup_end_steady_us
        self.warning_log_writer = warning_log_writer
        self.save_csv = save_csv
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
        self.max_receive_steady_delta_header_seq = None
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
            self.csv_writer.writerow(CSV_HEADER)
            self.csv_fh.flush()

    def _record_deltas(
        self,
        ros_header_stamp_delta_us,
        receive_steady_delta_us,
        header_seq,
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
            self.max_receive_steady_delta_header_seq = header_seq
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

    def write(self, msg):
        receive_system_us = system_time_us()
        receive_steady_us = steady_time_us()
        ros_header_stamp_us = ns_to_us(msg.header.stamp.to_nsec())
        header_seq = msg.header.seq

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
                    header_seq,
                    receive_system_us,
                )
            self._record_header_seq(header_seq)
            self._record_image_info(msg)
            self.prev_header_seq = header_seq

            if self.save_csv and self.csv_fh is not None and not self.csv_fh.closed:
                self.frame_index += 1
                self.csv_writer.writerow(
                    [
                        self.frame_index,
                        self.topic,
                        header_seq,
                        format_us_as_sec(ros_header_stamp_us),
                        format_optional_us_as_sec(ros_header_stamp_delta_us),
                        format_us_as_sec(receive_system_us),
                        format_us_as_sec(receive_steady_us),
                        format_optional_us_as_sec(receive_steady_delta_us),
                    ]
                )
                self.csv_fh.flush()

        if should_warn:
            warning_message = (
                "Image receive delta exceeded {:.3f}s: topic={}, seq={}, "
                "receive_steady_delta_sec={}, ros_header_stamp_delta_sec={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                header_seq,
                format_us_as_sec(receive_steady_delta_us),
                format_optional_us_as_sec(ros_header_stamp_delta_us),
            )
            rospy.logwarn("%s", warning_message)
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
            return (
                "Image topic no frame for more than {:.3f}s: topic={}, "
                "no_frame_duration_sec={}, last_header_seq={}, "
                "last_receive_system_ts_sec={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                format_us_as_sec(no_frame_duration_us),
                self.prev_header_seq if self.prev_header_seq is not None else "",
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
            return [
                self.topic,
                self.delta_count,
                format_optional_us_as_sec(self.receive_steady_delta_min_us),
                format_optional_us_as_sec(self.receive_steady_delta_max_us),
                format_optional_us_as_sec(receive_steady_delta_average_us),
                self.receive_steady_delta_warning_count,
                self.no_frame_warning_count,
                (
                    self.max_receive_steady_delta_header_seq
                    if self.max_receive_steady_delta_header_seq is not None
                    else ""
                ),
                format_optional_us_as_sec(self.max_receive_steady_delta_system_us),
                format_optional_us_as_sec(self.ros_header_stamp_delta_min_us),
                format_optional_us_as_sec(self.ros_header_stamp_delta_max_us),
                format_optional_us_as_sec(ros_header_stamp_delta_average_us),
                self.ros_header_stamp_non_positive_delta_count,
                self.header_seq_gap_count,
                self.header_seq_backward_count,
                self.header_seq_duplicate_count,
                self.image_info_change_count,
                self.zero_data_count,
            ]

    def close(self):
        with self.lock:
            self.closed = True
            if self.csv_fh is not None and not self.csv_fh.closed:
                self.csv_fh.flush()
                self.csv_fh.close()


class MultiImageReceiveStatsNode:
    def __init__(
        self,
        topics,
        output_dir,
        warning_interval_sec,
        queue_size,
        buff_size_mb,
        warmup_sec,
        save_csv,
    ):
        if not topics:
            raise ValueError("No image topics configured.")
        if warning_interval_sec <= 0:
            raise ValueError("warning_interval_sec must be greater than 0.")
        if queue_size == 0:
            raise ValueError("queue_size must be greater than 0, or -1 for unlimited.")
        if buff_size_mb <= 0:
            raise ValueError("buff_size must be greater than 0 MB.")
        if warmup_sec < 0:
            raise ValueError("warmup_sec must be greater than or equal to 0.")

        self.topics = topics
        self.output_dir = output_dir
        self.warning_interval_sec = warning_interval_sec
        self.warning_check_interval_sec = warning_check_interval_sec(warning_interval_sec)
        self.queue_size = queue_size
        self.buff_size_mb = buff_size_mb
        self.buff_size_bytes = buff_size_mb * 1024 * 1024
        self.warmup_sec = warmup_sec
        self.save_csv = save_csv
        self.loggers = {}
        self.subscribers = []
        self.warning_log_writer = None
        self.summary_file = os.path.join(self.output_dir, "summary.csv")
        self.metadata_file = os.path.join(self.output_dir, "metadata.json")
        self.summary_timer = None
        self.warning_timer = None
        self.summary_update_lock = threading.Lock()
        self.callback_condition = threading.Condition()
        self.active_callback_count = 0
        self.close_lock = threading.Lock()
        self.closed = False

        ensure_dir(self.output_dir)
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
            )

        self.write_metadata()
        self.write_summary()

        for topic in self.topics:
            self.subscribers.append(
                rospy.Subscriber(
                    topic,
                    Image,
                    self.image_callback,
                    callback_args=topic,
                    queue_size=self.queue_size,
                    buff_size=self.buff_size_bytes,
                )
            )

        self.summary_timer = rospy.Timer(
            rospy.Duration.from_sec(SUMMARY_UPDATE_INTERVAL_SEC),
            self.summary_timer_callback,
        )
        self.warning_timer = rospy.Timer(
            rospy.Duration.from_sec(self.warning_check_interval_sec),
            self.warning_timer_callback,
        )
        rospy.on_shutdown(self.close)
        rospy.loginfo("Recording image receive stats to directory: %s", self.output_dir)
        rospy.loginfo("Recording metadata to: %s", self.metadata_file)
        rospy.loginfo("Recording warning log to: %s", self.warning_log_writer.log_file)
        rospy.loginfo(
            "Recording cumulative summary every %.1fs to: %s",
            SUMMARY_UPDATE_INTERVAL_SEC,
            self.summary_file,
        )
        rospy.loginfo(
            "Checking no-frame warnings every %.3fs",
            self.warning_check_interval_sec,
        )
        rospy.loginfo(
            "Subscriber queue_size=%d buff_size=%dMB (%d bytes)",
            self.queue_size,
            self.buff_size_mb,
            self.buff_size_bytes,
        )
        rospy.loginfo("Per-frame CSV saving: %s", "enabled" if self.save_csv else "disabled")
        rospy.loginfo("Warmup time before recording deltas: %.3fs", self.warmup_sec)
        for topic in self.topics:
            if self.save_csv:
                rospy.loginfo(
                    "Subscribing image topic: %s -> %s",
                    topic,
                    self.loggers[topic].csv_file,
                )
            else:
                rospy.loginfo("Subscribing image topic: %s", topic)

    def write_metadata(self):
        write_json_atomic(
            self.metadata_file,
            {
                "started_at": datetime.now().isoformat(),
                "topics": self.topics,
                "output_dir": self.output_dir,
                "warning_interval_sec": self.warning_interval_sec,
                "warning_check_interval_sec": self.warning_check_interval_sec,
                "queue_size": self.queue_size,
                "buff_size_mb": self.buff_size_mb,
                "buff_size_bytes": self.buff_size_bytes,
                "warmup_sec": self.warmup_sec,
                "save_csv": self.save_csv,
                "ros_distro": os.environ.get("ROS_DISTRO", ""),
            },
        )

    def image_callback(self, msg, topic):
        with self.callback_condition:
            if self.closed:
                return
            self.active_callback_count += 1
        try:
            self.loggers[topic].write(msg)
        finally:
            with self.callback_condition:
                self.active_callback_count -= 1
                if self.active_callback_count == 0:
                    self.callback_condition.notify_all()

    def collect_summary_rows(self):
        return [self.loggers[topic].summary_row() for topic in self.topics]

    def write_summary(self):
        with self.summary_update_lock:
            rows = self.collect_summary_rows()
            write_csv_atomic(self.summary_file, SUMMARY_HEADER, rows)
            return rows

    def summary_timer_callback(self, _event):
        with self.summary_update_lock:
            if self.closed:
                return
            try:
                rows = self.collect_summary_rows()
                write_csv_atomic(self.summary_file, SUMMARY_HEADER, rows)
            except Exception as exc:
                rospy.logerr("Failed to update summary CSV %s: %s", self.summary_file, exc)

    def warning_timer_callback(self, _event):
        now_steady_us = steady_time_us()
        for topic in self.topics:
            with self.callback_condition:
                if self.closed:
                    return
            warning_message = self.loggers[topic].check_no_frame_warning(now_steady_us)
            if warning_message:
                rospy.logwarn("%s", warning_message)
                self.warning_log_writer.write(warning_message)

    def log_summary(self, rows):
        for row in rows:
            summary = dict(zip(SUMMARY_HEADER, row))
            rospy.loginfo(
                "Image receive summary: topic=%s, deltas=%s, "
                "receive_steady_delta_sec[min/max/average]=%s/%s/%s, delta_warnings=%s, "
                "no_frame_warnings=%s, "
                "ros_header_stamp_delta_sec[min/max/average]=%s/%s/%s, non_positive=%s, "
                "header_seq[gap/backward/duplicate]=%s/%s/%s, "
                "image_info_changes=%s, zero_data=%s",
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
                summary["header_seq_gap_count"],
                summary["header_seq_backward_count"],
                summary["header_seq_duplicate_count"],
                summary["image_info_change_count"],
                summary["zero_data_count"],
            )

    def close(self):
        with self.close_lock:
            with self.callback_condition:
                if self.closed:
                    return
                self.closed = True

            if self.summary_timer is not None:
                self.summary_timer.shutdown()
            if self.warning_timer is not None:
                self.warning_timer.shutdown()

            with self.callback_condition:
                while self.active_callback_count > 0:
                    self.callback_condition.wait()

            with self.summary_update_lock:
                rows = self.collect_summary_rows()
                try:
                    write_csv_atomic(self.summary_file, SUMMARY_HEADER, rows)
                except Exception as exc:
                    rospy.logerr("Failed to write final summary CSV %s: %s", self.summary_file, exc)
                self.log_summary(rows)

            for logger in self.loggers.values():
                logger.close()
            if self.warning_log_writer is not None:
                self.warning_log_writer.close()


def main():
    rospy.init_node("image_topic_receive_stats", anonymous=True)
    args = parse_args()

    topics_param = private_param_or_default("topics", args.topics, "")
    configured_topics = parse_topics(topics_param)
    if not configured_topics:
        raise ValueError(
            "No image topics configured. Set --topics or the private ROS parameter ~topics."
        )
    topics = resolve_topics(configured_topics)

    save_csv = parse_bool(private_param_or_default("save_csv", args.save_csv, DEFAULT_SAVE_CSV))
    disable_csv = parse_bool(private_param_or_default("disable_csv", args.disable_csv, False))
    if disable_csv:
        save_csv = False

    node = MultiImageReceiveStatsNode(
        topics=topics,
        output_dir=private_param_or_default("output_dir", args.output_dir, default_output_dir()),
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
    )
    rospy.spin()
    node.close()


if __name__ == "__main__":
    main()
