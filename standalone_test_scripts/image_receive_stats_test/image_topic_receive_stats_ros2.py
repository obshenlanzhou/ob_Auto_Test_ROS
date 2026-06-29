#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Subscribe multiple ROS2 image topics and write per-topic receive statistics to CSV.

Image topics must be provided explicitly through --topics or the ROS2 node
parameter topics.

Each topic is written to its own CSV file. For every received frame the script
records the ROS header timestamp, the host system and steady receive clocks,
and the delta from the previous frame on the same topic. A compact
summary.csv is always updated periodically, even when per-frame CSV saving is
disabled.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import json
import os
import re
import threading
import time

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


DEFAULT_WARNING_INTERVAL_SEC = 1.0
DEFAULT_QUEUE_SIZE = 10
DEFAULT_WARMUP_SEC = 2.0
DEFAULT_SAVE_CSV = True
DEFAULT_QOS = "sensor_data"
SUMMARY_UPDATE_INTERVAL_SEC = 10.0
MIN_WARNING_CHECK_INTERVAL_SEC = 0.05
MAX_WARNING_CHECK_INTERVAL_SEC = 1.0
NANOSECONDS_PER_MICROSECOND = 1000
MICROSECONDS_PER_SECOND = 1000000

CSV_HEADER = [
    "frame_index",
    "topic",
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
    "max_receive_steady_delta_frame_index",
    "max_receive_steady_delta_system_ts_sec",
    "ros_header_stamp_delta_min_sec",
    "ros_header_stamp_delta_max_sec",
    "ros_header_stamp_delta_average_sec",
    "ros_header_stamp_non_positive_delta_count",
    "image_info_change_count",
    "zero_data_count",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Record per-frame ROS2 image timestamps, host system receive timestamps, "
            "and steady-clock receive deltas to one CSV file per topic."
        )
    )
    parser.add_argument("--output_dir", default=None, help="Directory for output CSV files.")
    parser.add_argument(
        "--topics",
        default=None,
        help="Required comma-separated sensor_msgs/msg/Image topics. ROS param: topics:='[...]'",
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
            "ROS param: save_csv:=false"
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
        "--qos",
        default=None,
        choices=["sensor_data", "default", "reliable", "best_effort"],
        help="Subscriber QoS profile.",
    )
    return parser.parse_args(remove_ros_args()[1:])


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


def get_parameter_or_default(node, name, command_line_value, default_value):
    if command_line_value is not None:
        return command_line_value
    return node.get_parameter(name).value if node.has_parameter(name) else default_value


def default_output_dir():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.abspath("image_receive_stats_ros2_{}".format(timestamp))


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
        self.no_frame_warning_active = False
        self.no_frame_warning_count = 0
        self.delta_count = 0
        self.receive_steady_delta_min_us = None
        self.receive_steady_delta_max_us = None
        self.receive_steady_delta_total_us = 0
        self.receive_steady_delta_warning_count = 0
        self.max_receive_steady_delta_frame_index = None
        self.max_receive_steady_delta_system_us = None
        self.ros_header_stamp_delta_min_us = None
        self.ros_header_stamp_delta_max_us = None
        self.ros_header_stamp_positive_delta_count = 0
        self.ros_header_stamp_positive_delta_total_us = 0
        self.ros_header_stamp_non_positive_delta_count = 0
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
        frame_index,
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
            self.max_receive_steady_delta_frame_index = frame_index
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

    def _record_image_info(self, msg):
        image_info = (msg.width, msg.height, msg.encoding, msg.step, len(msg.data))
        if self.prev_image_info is not None and image_info != self.prev_image_info:
            self.image_info_change_count += 1
        self.prev_image_info = image_info
        if len(msg.data) == 0:
            self.zero_data_count += 1

    def write(self, node_logger, msg):
        receive_system_us = system_time_us()
        receive_steady_us = steady_time_us()
        ros_header_stamp_us = stamp_to_us(msg.header.stamp)

        with self.lock:
            if self.closed:
                return

            if receive_steady_us < self.warmup_end_steady_us:
                self.prev_ros_header_stamp_us = None
                self.prev_receive_steady_us = None
                self.prev_receive_system_us = None
                self.prev_image_info = None
                self.no_frame_warning_active = False
                return

            self.frame_index += 1
            frame_index = self.frame_index

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
                    frame_index,
                    receive_system_us,
                )
            self._record_image_info(msg)

            if self.save_csv and self.csv_fh is not None and not self.csv_fh.closed:
                self.csv_writer.writerow(
                    [
                        frame_index,
                        self.topic,
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
                "Image receive delta exceeded {:.3f}s: topic={}, frame_index={}, "
                "receive_steady_delta_sec={}, ros_header_stamp_delta_sec={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                frame_index,
                format_us_as_sec(receive_steady_delta_us),
                format_optional_us_as_sec(ros_header_stamp_delta_us),
            )
            node_logger.warn(warning_message)
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
                "no_frame_duration_sec={}, last_frame_index={}, "
                "last_receive_system_ts_sec={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                format_us_as_sec(no_frame_duration_us),
                self.frame_index if self.frame_index > 0 else "",
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
                    self.max_receive_steady_delta_frame_index
                    if self.max_receive_steady_delta_frame_index is not None
                    else ""
                ),
                format_optional_us_as_sec(self.max_receive_steady_delta_system_us),
                format_optional_us_as_sec(self.ros_header_stamp_delta_min_us),
                format_optional_us_as_sec(self.ros_header_stamp_delta_max_us),
                format_optional_us_as_sec(ros_header_stamp_delta_average_us),
                self.ros_header_stamp_non_positive_delta_count,
                self.image_info_change_count,
                self.zero_data_count,
            ]

    def close(self):
        with self.lock:
            self.closed = True
            if self.csv_fh is not None and not self.csv_fh.closed:
                self.csv_fh.flush()
                self.csv_fh.close()


class MultiImageReceiveStatsNode(Node):
    def __init__(
        self,
        topics,
        output_dir,
        warning_interval_sec,
        queue_size,
        qos,
        warmup_sec,
        save_csv,
    ):
        super().__init__("image_topic_receive_stats_ros2")

        if not topics:
            raise ValueError("No image topics configured.")
        if warning_interval_sec <= 0:
            raise ValueError("warning_interval_sec must be greater than 0.")
        if queue_size == 0:
            raise ValueError("queue_size must be greater than 0, or -1 for unlimited.")
        if warmup_sec < 0:
            raise ValueError("warmup_sec must be greater than or equal to 0.")

        self.topics = [self.resolve_topic_name(topic) for topic in topics]
        duplicate_topics = sorted({topic for topic in self.topics if self.topics.count(topic) > 1})
        if duplicate_topics:
            raise ValueError(
                "Duplicate image topics after ROS name resolution: {}".format(
                    ", ".join(duplicate_topics)
                )
            )

        self.output_dir = output_dir
        self.warning_interval_sec = warning_interval_sec
        self.warning_check_interval_sec = warning_check_interval_sec(warning_interval_sec)
        self.queue_size = queue_size
        self.qos = qos
        self.qos_profile = make_qos_profile(qos, queue_size)
        self.warmup_sec = warmup_sec
        self.save_csv = save_csv
        self.loggers = {}
        self.subscribers = []
        self.warning_log_writer = None
        self.summary_file = os.path.join(self.output_dir, "summary.csv")
        self.metadata_file = os.path.join(self.output_dir, "metadata.json")
        self.summary_update_lock = threading.Lock()
        self.summary_future_lock = threading.Lock()
        self.summary_future = None
        self.summary_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="image_receive_summary",
        )
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
            self.warning_check_interval_sec,
            self.warning_timer_callback,
        )
        logger = self.get_logger()
        logger.info("Recording image receive stats to directory: {}".format(self.output_dir))
        logger.info("Recording metadata to: {}".format(self.metadata_file))
        logger.info("Recording warning log to: {}".format(self.warning_log_writer.log_file))
        logger.info(
            "Recording cumulative summary every {:.1f}s to: {}".format(
                SUMMARY_UPDATE_INTERVAL_SEC,
                self.summary_file,
            )
        )
        logger.info(
            "Checking no-frame warnings every {:.3f}s".format(
                self.warning_check_interval_sec
            )
        )
        logger.info("Subscriber queue_size={} qos={}".format(self.queue_size, self.qos))
        logger.info("Per-frame CSV saving: {}".format("enabled" if self.save_csv else "disabled"))
        logger.info("Warmup time before recording deltas: {:.3f}s".format(self.warmup_sec))
        for topic in self.topics:
            if self.save_csv:
                logger.info(
                    "Subscribing image topic: {} -> {}".format(
                        topic,
                        self.loggers[topic].csv_file,
                    )
                )
            else:
                logger.info("Subscribing image topic: {}".format(topic))

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
                "qos": self.qos,
                "warmup_sec": self.warmup_sec,
                "save_csv": self.save_csv,
                "ros_distro": os.environ.get("ROS_DISTRO", ""),
                "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION", ""),
            },
        )

    def image_callback(self, msg, topic):
        with self.callback_condition:
            if self.closed:
                return
            self.active_callback_count += 1
        try:
            self.loggers[topic].write(self.get_logger(), msg)
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

    def summary_timer_callback(self):
        with self.summary_future_lock:
            if self.closed:
                return
            if self.summary_future is not None:
                if not self.summary_future.done():
                    return
                try:
                    self.summary_future.result()
                except Exception as exc:
                    self.get_logger().error(
                        "Failed to update summary CSV {}: {}".format(
                            self.summary_file,
                            exc,
                        )
                    )
            self.summary_future = self.summary_executor.submit(self.write_summary)

    def wait_for_pending_summary_update(self):
        with self.summary_future_lock:
            summary_future = self.summary_future
            self.summary_future = None
        if summary_future is not None:
            summary_future.result()

    def warning_timer_callback(self):
        now_steady_us = steady_time_us()
        for topic in self.topics:
            with self.callback_condition:
                if self.closed:
                    return
            warning_message = self.loggers[topic].check_no_frame_warning(now_steady_us)
            if warning_message:
                self.get_logger().warn(warning_message)
                self.warning_log_writer.write(warning_message)

    def log_summary(self, rows):
        for row in rows:
            summary = dict(zip(SUMMARY_HEADER, row))
            self.get_logger().info(
                "Image receive summary: topic={}, deltas={}, "
                "receive_steady_delta_sec[min/max/average]={}/{}/{}, delta_warnings={}, "
                "no_frame_warnings={}, "
                "ros_header_stamp_delta_sec[min/max/average]={}/{}/{}, non_positive={}, "
                "image_info_changes={}, zero_data={}".format(
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
                    summary["image_info_change_count"],
                    summary["zero_data_count"],
                )
            )

    def close(self):
        with self.close_lock:
            with self.callback_condition:
                if self.closed:
                    return
                self.closed = True

            self.summary_timer.cancel()
            self.warning_timer.cancel()

            with self.callback_condition:
                while self.active_callback_count > 0:
                    self.callback_condition.wait()

            try:
                self.wait_for_pending_summary_update()
            except Exception as exc:
                self.get_logger().error(
                    "Failed to complete pending summary CSV update {}: {}".format(
                        self.summary_file,
                        exc,
                    )
                )

            with self.summary_update_lock:
                rows = self.collect_summary_rows()
                try:
                    write_csv_atomic(self.summary_file, SUMMARY_HEADER, rows)
                except Exception as exc:
                    self.get_logger().error(
                        "Failed to write final summary CSV {}: {}".format(
                            self.summary_file,
                            exc,
                        )
                    )
                if rclpy.ok():
                    self.log_summary(rows)

            for logger in self.loggers.values():
                logger.close()
            if self.warning_log_writer is not None:
                self.warning_log_writer.close()
            self.summary_executor.shutdown(wait=True)


def declare_parameters(node):
    node.declare_parameter("topics", "")
    node.declare_parameter("output_dir", "")
    node.declare_parameter("warning_interval_sec", DEFAULT_WARNING_INTERVAL_SEC)
    node.declare_parameter("warmup_sec", DEFAULT_WARMUP_SEC)
    node.declare_parameter("save_csv", DEFAULT_SAVE_CSV)
    node.declare_parameter("disable_csv", False)
    node.declare_parameter("queue_size", DEFAULT_QUEUE_SIZE)
    node.declare_parameter("qos", DEFAULT_QOS)


def main():
    rclpy.init()
    args = parse_args()

    parameter_node = Node("image_topic_receive_stats_ros2_params")
    declare_parameters(parameter_node)

    try:
        topics_param = get_parameter_or_default(parameter_node, "topics", args.topics, "")
        configured_topics = parse_topics(topics_param)
        if not configured_topics:
            raise ValueError(
                "No image topics configured. Set --topics or the ROS2 parameter topics."
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
            output_dir = default_output_dir()

        node = MultiImageReceiveStatsNode(
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
        )
    finally:
        parameter_node.destroy_node()

    try:
        try:
            rclpy.spin(node)
        except KeyboardInterrupt:
            pass
    finally:
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
