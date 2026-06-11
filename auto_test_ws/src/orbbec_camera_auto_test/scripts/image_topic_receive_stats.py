#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Subscribe multiple ROS image topics and write per-topic receive statistics to CSV.

Image topics must be provided explicitly through --topics or the private ROS
parameter ~topics.

Each topic is written to its own CSV file. For every received frame the script
records the ROS message timestamp, the host wall and monotonic receive clocks,
and the interval from the previous frame on the same topic. A compact
summary.csv is always updated periodically, even when per-frame CSV saving is
disabled.
"""

import argparse
import csv
from datetime import datetime
import os
import re
import threading
import time

import rospy
from sensor_msgs.msg import Image


DEFAULT_WARNING_INTERVAL_SEC = 1.0
DEFAULT_QUEUE_SIZE = 10
DEFAULT_WARMUP_SEC = 2.0
DEFAULT_SAVE_CSV = True
SUMMARY_UPDATE_INTERVAL_SEC = 10.0
MIN_WARNING_CHECK_INTERVAL_SEC = 0.05
MAX_WARNING_CHECK_INTERVAL_SEC = 1.0
NANOSECONDS_PER_SECOND = 1000000000

CSV_HEADER = [
    "frame_index",
    "topic",
    "header_seq",
    "message_stamp",
    "message_stamp_interval_sec",
    "receive_wall_time",
    "receive_monotonic",
    "receive_monotonic_interval_sec",
]

SUMMARY_HEADER = [
    "topic",
    "interval_count",
    "receive_monotonic_interval_min_sec",
    "receive_monotonic_interval_max_sec",
    "receive_monotonic_interval_average_sec",
    "receive_monotonic_interval_warning_count",
    "no_frame_warning_count",
    "max_receive_monotonic_interval_header_seq",
    "max_receive_monotonic_interval_wall_time",
    "message_stamp_interval_min_sec",
    "message_stamp_interval_max_sec",
    "message_stamp_interval_average_sec",
    "message_stamp_non_positive_interval_count",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Record per-frame ROS image timestamps, host wall receive timestamps, "
            "and monotonic receive intervals to one CSV file per topic."
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
        help="Warn when consecutive receive intervals on a topic exceed this value.",
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


def seconds_to_ns(seconds):
    return int(round(seconds * NANOSECONDS_PER_SECOND))


def monotonic_time_ns():
    if hasattr(time, "monotonic_ns"):
        return time.monotonic_ns()
    return seconds_to_ns(time.monotonic())


def wall_time_ns():
    if hasattr(time, "time_ns"):
        return time.time_ns()
    return seconds_to_ns(time.time())


def format_ns_as_sec(ns_value):
    sign = "-" if ns_value < 0 else ""
    absolute_ns = abs(ns_value)
    return "{}{}.{:09d}".format(
        sign,
        absolute_ns // NANOSECONDS_PER_SECOND,
        absolute_ns % NANOSECONDS_PER_SECOND,
    )


def format_optional_ns_as_sec(value):
    if value is None or value == "":
        return ""
    return format_ns_as_sec(value)


def rounded_average_ns(total_ns, count):
    if count <= 0:
        return None
    sign = -1 if total_ns < 0 else 1
    return sign * ((abs(total_ns) + count // 2) // count)


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
        warmup_end_monotonic_ns,
        warning_log_writer,
        save_csv,
    ):
        self.topic = topic
        self.csv_file = csv_file
        self.warning_interval_sec = warning_interval_sec
        self.warning_interval_ns = seconds_to_ns(warning_interval_sec)
        self.warmup_end_monotonic_ns = warmup_end_monotonic_ns
        self.warning_log_writer = warning_log_writer
        self.save_csv = save_csv
        self.lock = threading.Lock()
        self.frame_index = 0
        self.prev_msg_stamp_ns = None
        self.prev_receive_monotonic_ns = None
        self.prev_receive_wall_time_ns = None
        self.prev_header_seq = None
        self.no_frame_warning_active = False
        self.no_frame_warning_count = 0
        self.interval_count = 0
        self.receive_interval_min_ns = None
        self.receive_interval_max_ns = None
        self.receive_interval_total_ns = 0
        self.receive_interval_warning_count = 0
        self.max_receive_interval_header_seq = None
        self.max_receive_interval_wall_time_ns = None
        self.msg_stamp_interval_min_ns = None
        self.msg_stamp_interval_max_ns = None
        self.msg_stamp_positive_interval_count = 0
        self.msg_stamp_positive_interval_total_ns = 0
        self.msg_stamp_non_positive_interval_count = 0
        self.closed = False

        self.csv_fh = None
        self.csv_writer = None
        if self.save_csv:
            self.csv_fh = open(self.csv_file, "w", newline="")
            self.csv_writer = csv.writer(self.csv_fh)
            self.csv_writer.writerow(CSV_HEADER)
            self.csv_fh.flush()

    def _record_intervals(
        self,
        msg_stamp_interval_ns,
        receive_monotonic_interval_ns,
        header_seq,
        receive_wall_time_ns,
    ):
        self.interval_count += 1

        if (
            self.receive_interval_min_ns is None
            or receive_monotonic_interval_ns < self.receive_interval_min_ns
        ):
            self.receive_interval_min_ns = receive_monotonic_interval_ns
        if (
            self.receive_interval_max_ns is None
            or receive_monotonic_interval_ns > self.receive_interval_max_ns
        ):
            self.receive_interval_max_ns = receive_monotonic_interval_ns
            self.max_receive_interval_header_seq = header_seq
            self.max_receive_interval_wall_time_ns = receive_wall_time_ns
        self.receive_interval_total_ns += receive_monotonic_interval_ns

        if receive_monotonic_interval_ns > self.warning_interval_ns:
            self.receive_interval_warning_count += 1

        if (
            self.msg_stamp_interval_min_ns is None
            or msg_stamp_interval_ns < self.msg_stamp_interval_min_ns
        ):
            self.msg_stamp_interval_min_ns = msg_stamp_interval_ns
        if (
            self.msg_stamp_interval_max_ns is None
            or msg_stamp_interval_ns > self.msg_stamp_interval_max_ns
        ):
            self.msg_stamp_interval_max_ns = msg_stamp_interval_ns
        if msg_stamp_interval_ns > 0:
            self.msg_stamp_positive_interval_count += 1
            self.msg_stamp_positive_interval_total_ns += msg_stamp_interval_ns
        else:
            self.msg_stamp_non_positive_interval_count += 1

    def write(self, msg):
        receive_wall_time_ns = wall_time_ns()
        receive_monotonic_ns = monotonic_time_ns()
        msg_stamp_ns = msg.header.stamp.to_nsec()

        with self.lock:
            if self.closed:
                return

            if receive_monotonic_ns < self.warmup_end_monotonic_ns:
                self.prev_msg_stamp_ns = None
                self.prev_receive_monotonic_ns = None
                self.prev_receive_wall_time_ns = None
                self.prev_header_seq = None
                self.no_frame_warning_active = False
                return

            msg_stamp_interval_ns = ""
            receive_monotonic_interval_ns = ""
            if self.prev_msg_stamp_ns is not None:
                msg_stamp_interval_ns = msg_stamp_ns - self.prev_msg_stamp_ns
            if self.prev_receive_monotonic_ns is not None:
                receive_monotonic_interval_ns = (
                    receive_monotonic_ns - self.prev_receive_monotonic_ns
                )

            self.prev_msg_stamp_ns = msg_stamp_ns
            self.prev_receive_monotonic_ns = receive_monotonic_ns
            self.prev_receive_wall_time_ns = receive_wall_time_ns
            self.prev_header_seq = msg.header.seq
            self.no_frame_warning_active = False

            should_warn = receive_monotonic_interval_ns != "" and (
                receive_monotonic_interval_ns > self.warning_interval_ns
            )
            if receive_monotonic_interval_ns != "":
                self._record_intervals(
                    msg_stamp_interval_ns,
                    receive_monotonic_interval_ns,
                    msg.header.seq,
                    receive_wall_time_ns,
                )

            if self.save_csv and self.csv_fh is not None and not self.csv_fh.closed:
                self.frame_index += 1
                self.csv_writer.writerow(
                    [
                        self.frame_index,
                        self.topic,
                        msg.header.seq,
                        format_ns_as_sec(msg_stamp_ns),
                        format_optional_ns_as_sec(msg_stamp_interval_ns),
                        format_ns_as_sec(receive_wall_time_ns),
                        format_ns_as_sec(receive_monotonic_ns),
                        format_optional_ns_as_sec(receive_monotonic_interval_ns),
                    ]
                )
                self.csv_fh.flush()

        if should_warn:
            warning_message = (
                "Image receive interval exceeded {:.3f}s: topic={}, seq={}, "
                "receive_monotonic_interval={}s, message_stamp_interval={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                msg.header.seq,
                format_ns_as_sec(receive_monotonic_interval_ns),
                format_optional_ns_as_sec(msg_stamp_interval_ns),
            )
            rospy.logwarn("%s", warning_message)
            self.warning_log_writer.write(warning_message)

    def check_no_frame_warning(self, now_monotonic_ns):
        with self.lock:
            if self.closed or now_monotonic_ns < self.warmup_end_monotonic_ns:
                return None

            if self.prev_receive_monotonic_ns is None:
                no_frame_duration_ns = now_monotonic_ns - self.warmup_end_monotonic_ns
            else:
                no_frame_duration_ns = now_monotonic_ns - self.prev_receive_monotonic_ns

            if (
                no_frame_duration_ns <= self.warning_interval_ns
                or self.no_frame_warning_active
            ):
                return None

            self.no_frame_warning_active = True
            self.no_frame_warning_count += 1
            return (
                "Image topic no frame for more than {:.3f}s: topic={}, "
                "no_frame_duration={}s, last_header_seq={}, "
                "last_receive_wall_time={}"
            ).format(
                self.warning_interval_sec,
                self.topic,
                format_ns_as_sec(no_frame_duration_ns),
                self.prev_header_seq if self.prev_header_seq is not None else "",
                format_optional_ns_as_sec(self.prev_receive_wall_time_ns),
            )

    def summary_row(self):
        with self.lock:
            receive_interval_average_ns = rounded_average_ns(
                self.receive_interval_total_ns,
                self.interval_count,
            )
            msg_stamp_interval_average_ns = rounded_average_ns(
                self.msg_stamp_positive_interval_total_ns,
                self.msg_stamp_positive_interval_count,
            )
            return [
                self.topic,
                self.interval_count,
                format_optional_ns_as_sec(self.receive_interval_min_ns),
                format_optional_ns_as_sec(self.receive_interval_max_ns),
                format_optional_ns_as_sec(receive_interval_average_ns),
                self.receive_interval_warning_count,
                self.no_frame_warning_count,
                (
                    self.max_receive_interval_header_seq
                    if self.max_receive_interval_header_seq is not None
                    else ""
                ),
                format_optional_ns_as_sec(self.max_receive_interval_wall_time_ns),
                format_optional_ns_as_sec(self.msg_stamp_interval_min_ns),
                format_optional_ns_as_sec(self.msg_stamp_interval_max_ns),
                format_optional_ns_as_sec(msg_stamp_interval_average_ns),
                self.msg_stamp_non_positive_interval_count,
            ]

    def close(self):
        with self.lock:
            self.closed = True
            if self.csv_fh is not None and not self.csv_fh.closed:
                self.csv_fh.flush()
                self.csv_fh.close()


class MultiImageReceiveStatsNode:
    def __init__(self, topics, output_dir, warning_interval_sec, queue_size, warmup_sec, save_csv):
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
        self.loggers = {}
        self.subscribers = []
        self.warning_log_writer = None
        self.summary_file = os.path.join(self.output_dir, "summary.csv")
        self.summary_timer = None
        self.warning_timer = None
        self.summary_update_lock = threading.Lock()
        self.callback_condition = threading.Condition()
        self.active_callback_count = 0
        self.close_lock = threading.Lock()
        self.closed = False

        ensure_dir(self.output_dir)
        self.warning_log_writer = WarningLogWriter(os.path.join(self.output_dir, "warnings.log"))
        warmup_end_monotonic_ns = monotonic_time_ns() + seconds_to_ns(self.warmup_sec)

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
                warmup_end_monotonic_ns,
                self.warning_log_writer,
                self.save_csv,
            )

        self.write_summary()

        for topic in self.topics:
            self.subscribers.append(
                rospy.Subscriber(
                    topic,
                    Image,
                    self.image_callback,
                    callback_args=topic,
                    queue_size=self.queue_size,
                    buff_size=2 ** 24,
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
        rospy.loginfo("Per-frame CSV saving: %s", "enabled" if self.save_csv else "disabled")
        rospy.loginfo("Warmup time before recording intervals: %.3fs", self.warmup_sec)
        for topic in self.topics:
            if self.save_csv:
                rospy.loginfo(
                    "Subscribing image topic: %s -> %s",
                    topic,
                    self.loggers[topic].csv_file,
                )
            else:
                rospy.loginfo("Subscribing image topic: %s", topic)

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
        now_monotonic_ns = monotonic_time_ns()
        for topic in self.topics:
            with self.callback_condition:
                if self.closed:
                    return
            warning_message = self.loggers[topic].check_no_frame_warning(now_monotonic_ns)
            if warning_message:
                rospy.logwarn("%s", warning_message)
                self.warning_log_writer.write(warning_message)

    def log_summary(self, rows):
        for row in rows:
            summary = dict(zip(SUMMARY_HEADER, row))
            rospy.loginfo(
                "Image receive summary: topic=%s, intervals=%s, "
                "receive_sec[min/max/average]=%s/%s/%s, interval_warnings=%s, "
                "no_frame_warnings=%s, "
                "message_stamp_sec[min/max/average]=%s/%s/%s, non_positive=%s",
                summary["topic"],
                summary["interval_count"],
                summary["receive_monotonic_interval_min_sec"],
                summary["receive_monotonic_interval_max_sec"],
                summary["receive_monotonic_interval_average_sec"],
                summary["receive_monotonic_interval_warning_count"],
                summary["no_frame_warning_count"],
                summary["message_stamp_interval_min_sec"],
                summary["message_stamp_interval_max_sec"],
                summary["message_stamp_interval_average_sec"],
                summary["message_stamp_non_positive_interval_count"],
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
        warmup_sec=float(
            private_param_or_default("warmup_sec", args.warmup_sec, DEFAULT_WARMUP_SEC)
        ),
        save_csv=save_csv,
    )
    rospy.spin()
    node.close()


if __name__ == "__main__":
    main()
