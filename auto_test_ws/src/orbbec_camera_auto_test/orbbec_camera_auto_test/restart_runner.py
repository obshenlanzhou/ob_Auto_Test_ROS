from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .functional_runner import _parse_launch_args, _require_detected_camera
from .performance_runner import _select_performance_scenarios, _wait_for_camera_ready
from .profile_loader import CameraProfile, TopicSpec, load_camera_profile
from .reporter import append_log, ensure_dir, write_json, write_markdown
from .ros_utils import RosHarness, make_qos_profile, resolve_message_type
from .session import TestSession


def _parse_duration_value(value: Any, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, (int, float)):
        duration = float(value)
    else:
        raw = str(value).strip().lower()
        multiplier = 1.0
        if raw.endswith("s"):
            raw = raw[:-1]
        elif raw.endswith("m"):
            raw = raw[:-1]
            multiplier = 60.0
        elif raw.endswith("h"):
            raw = raw[:-1]
            multiplier = 3600.0
        duration = float(raw) * multiplier
    if duration <= 0.0:
        raise ValueError("duration values must be > 0")
    return duration


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


def _topic_label(topic_name: str) -> str:
    return topic_name.strip("/").replace("/", "_") or "image"


def _format_topic_name(topic_name: str, camera_name: str) -> str:
    return (
        topic_name.replace("{camera}", camera_name)
        .replace("{camera_name}", camera_name)
        .replace("${camera}", camera_name)
        .replace("${camera_name}", camera_name)
    )


def _default_image_topic(camera_name: str) -> TopicSpec:
    return TopicSpec(
        name=f"/{camera_name}/color/image_raw",
        type="sensor_msgs/msg/Image",
        mode="message",
        validator="image",
        timeout=10.0,
    )


def _build_restart_launch_args(args, profile: CameraProfile | None) -> Dict[str, Any]:
    launch_args = dict(profile.default_launch_args) if profile is not None else {}
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


def _image_topics_from_args(
    args, profile: CameraProfile | None, launch_args: Dict[str, Any]
) -> List[TopicSpec]:
    scenario_topics: List[TopicSpec] = []
    if args.performance_scenario:
        if profile is None:
            raise ValueError("--performance-scenario requires --profile in restart mode")
        selected = _select_performance_scenarios(profile, args.performance_scenario)
        if selected:
            launch_args.update(selected[0].launch_args)
            scenario_topics = selected[0].topics
    elif profile is not None and profile.performance_scenarios:
        scenario_topics = profile.performance_scenarios[0].topics

    camera_name = str(launch_args.get("camera_name", "camera"))
    stream_timeout = _parse_duration_value(args.stream_timeout, 60.0)
    raw_topics = [item.strip() for item in args.image_topic or [] if item.strip()]
    if raw_topics:
        return [
            TopicSpec(
                name=_format_topic_name(topic, camera_name),
                type="sensor_msgs/msg/Image",
                mode="message",
                validator="image",
                timeout=stream_timeout,
            )
            for topic in raw_topics
        ]

    image_topics = [
        TopicSpec(
            name=_format_topic_name(topic.name, camera_name),
            type=topic.type or "sensor_msgs/msg/Image",
            mode="message",
            validator="image",
            paired_topic=topic.paired_topic,
            timeout=stream_timeout,
            qos=topic.qos,
        )
        for topic in scenario_topics
        if topic.validator == "image" and (topic.type in {"", "sensor_msgs/msg/Image"})
    ]
    if image_topics:
        return image_topics
    return [_default_image_topic(camera_name)]


class StableImageMonitor:
    def __init__(
        self,
        harness: RosHarness,
        topics: List[TopicSpec],
        log_path: Path,
        emit_status,
    ) -> None:
        self.harness = harness
        self.topics = topics
        self.log_path = log_path
        self.emit_status = emit_status
        self.state: Dict[str, Dict[str, Any]] = {}
        self.subscriptions = []
        for topic in topics:
            self.state[topic.name] = {
                "message_count": 0,
                "first_message_at": None,
                "last_message_at": None,
                "last_width": 0,
                "last_height": 0,
            }
            msg_type = resolve_message_type(topic.type or "sensor_msgs/msg/Image")
            self.subscriptions.append(
                harness.node.create_subscription(
                    msg_type,
                    topic.name,
                    lambda msg, topic_name=topic.name: self._on_message(topic_name, msg),
                    make_qos_profile(topic.qos),
                )
            )

    def _on_message(self, topic_name: str, message) -> None:
        now = time.monotonic()
        width = int(getattr(message, "width", 0) or 0)
        height = int(getattr(message, "height", 0) or 0)
        item = self.state[topic_name]
        item["message_count"] += 1
        item["last_message_at"] = now
        item["last_width"] = width
        item["last_height"] = height
        if item["first_message_at"] is None and width > 0 and height > 0:
            item["first_message_at"] = now
            append_log(self.log_path, f"[RESTART][TOPIC] first image on {topic_name}: {width}x{height}")
            self.emit_status(f"[RESTART][TOPIC] first image on {topic_name}: {width}x{height}")

    def close(self) -> None:
        for subscription in self.subscriptions:
            self.harness.node.destroy_subscription(subscription)
        self.subscriptions = []

    def snapshot(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        rows = []
        for topic_name, item in self.state.items():
            last_message_at = item["last_message_at"]
            first_message_at = item["first_message_at"]
            rows.append(
                {
                    "name": topic_name,
                    "message_count": item["message_count"],
                    "width": item["last_width"],
                    "height": item["last_height"],
                    "seconds_since_last_message": (
                        now - last_message_at if last_message_at is not None else None
                    ),
                    "stable_seconds": (
                        now - first_message_at if first_message_at is not None else 0.0
                    ),
                }
            )
        return rows

    def stable_for(self, stable_seconds: float, max_gap_seconds: float) -> bool:
        now = time.monotonic()
        for item in self.state.values():
            first_message_at = item["first_message_at"]
            last_message_at = item["last_message_at"]
            if first_message_at is None or last_message_at is None:
                return False
            if now - last_message_at > max_gap_seconds:
                item["first_message_at"] = None
                return False
            if now - first_message_at < stable_seconds:
                return False
        return True


def _wait_for_stable_streams(
    *,
    session: TestSession,
    harness: RosHarness,
    topics: List[TopicSpec],
    stable_seconds: float,
    timeout: float,
    max_gap_seconds: float,
    log_path: Path,
    emit_status,
) -> tuple[bool, List[Dict[str, Any]], str]:
    monitor = StableImageMonitor(harness, topics, log_path, emit_status)
    topic_names = ", ".join(topic.name for topic in topics)
    emit_status(
        f"waiting for stable image streams for {stable_seconds:.1f}s: {topic_names}"
    )
    deadline = time.monotonic() + timeout
    next_progress_log = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline:
            session.assert_running()
            harness.spin_once(0.1)
            if monitor.stable_for(stable_seconds, max_gap_seconds):
                snapshot = monitor.snapshot()
                return True, snapshot, "image streams are stable"
            if time.monotonic() >= next_progress_log:
                parts = []
                for item in monitor.snapshot():
                    stable = item["stable_seconds"]
                    count = item["message_count"]
                    gap = item["seconds_since_last_message"]
                    gap_text = "none" if gap is None else f"{gap:.2f}s"
                    parts.append(
                        f"{item['name']} count={count} stable={stable:.1f}s gap={gap_text}"
                    )
                emit_status("[RESTART][WAIT] " + "; ".join(parts))
                next_progress_log = time.monotonic() + 5.0
        snapshot = monitor.snapshot()
        return False, snapshot, f"image streams were not stable within {timeout:.1f}s"
    finally:
        monitor.close()


def _build_restart_summary(result: Dict[str, Any]) -> List[str]:
    lines = [
        f"# Launch Restart Test Summary: {result['profile_name']}",
        "",
        "## Overview",
        "",
        "| Item | Value |",
        "| --- | --- |",
        f"| Status | {result.get('status', '')} |",
        f"| Launch file | {result.get('launch_file', '')} |",
        f"| Camera name | {result.get('camera_name', '')} |",
        f"| Successful restarts | {result.get('successful_restarts', 0)} |",
        f"| Launch attempts | {result.get('launch_attempts', 0)} |",
        f"| Duration seconds | {result.get('duration_seconds', 0)} |",
        "",
        "## Attempts",
        "",
        "| Attempt | Status | Message | Stable Seconds | Topics |",
        "| --- | --- | --- | --- | --- |",
    ]
    for attempt in result.get("attempts", []):
        topics = ", ".join(
            f"{item.get('name')} count={item.get('message_count', 0)}"
            for item in attempt.get("topics", [])
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(attempt.get("attempt", "")),
                    str(attempt.get("status", "")),
                    str(attempt.get("message", "")).replace("|", "\\|"),
                    f"{float(attempt.get('stable_seconds', 0.0) or 0.0):.1f}",
                    topics.replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return lines


def run_restart_test(args) -> int:
    results_dir = ensure_dir(Path(args.results_dir).resolve())
    launch_log_path = results_dir / "launch.log"
    restart_log_path = results_dir / "restart.log"
    emit_status = _make_status_logger(restart_log_path)

    profile = (
        load_camera_profile(args.profile, profile_type="performance")
        if str(args.profile or "").strip()
        else None
    )
    launch_file = args.launch_file or (profile.launch_file if profile is not None else "")
    if not launch_file:
        raise ValueError("--launch-file is required when --profile is not set")
    base_launch_args = _build_restart_launch_args(args, profile)
    topics = _image_topics_from_args(args, profile, base_launch_args)
    camera_name = str(base_launch_args.get("camera_name", "camera"))
    duration_seconds = _parse_duration_value(args.duration, 300.0)
    stable_seconds = _parse_duration_value(args.stable_seconds, 10.0)
    stream_timeout = _parse_duration_value(args.stream_timeout, 60.0)
    max_gap_seconds = _parse_duration_value(args.max_gap_seconds, 1.5)
    deadline = time.monotonic() + duration_seconds

    result: Dict[str, Any] = {
        "profile_name": profile.profile_name if profile is not None else "",
        "launch_file": launch_file,
        "camera_name": camera_name,
        "launch_args": dict(base_launch_args),
        "image_topics": [asdict(topic) for topic in topics],
        "duration_seconds": duration_seconds,
        "successful_restarts": 0,
        "launch_attempts": 0,
        "stable_seconds_required": stable_seconds,
        "stream_timeout_seconds": stream_timeout,
        "max_gap_seconds": max_gap_seconds,
        "status": "passed",
        "attempts": [],
    }
    write_json(results_dir / "launch_args.json", {"launch_file": launch_file, "launch_args": base_launch_args})

    with RosHarness("orbbec_camera_restart_test") as harness:
        try:
            _require_detected_camera(args.driver_setup, emit_status)
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["error"] = str(exc)
            emit_status(f"restart preflight failed: {exc}")
            write_json(results_dir / "result.json", result)
            write_markdown(results_dir / "summary.md", _build_restart_summary(result))
            return 1

        attempt_index = 0
        while time.monotonic() < deadline:
            attempt_index += 1
            attempt_dir = ensure_dir(results_dir / "attempts" / f"{attempt_index:03d}")
            session = TestSession(
                launch_file=launch_file,
                launch_args=base_launch_args,
                work_dir=attempt_dir,
                log_path=launch_log_path,
                driver_setup=args.driver_setup,
                status_callback=emit_status,
            )
            attempt = {
                "attempt": attempt_index,
                "status": "running",
                "message": "",
                "topics": [],
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ended_at": "",
                "stable_seconds": 0.0,
            }
            result["attempts"].append(attempt)
            result["launch_attempts"] = attempt_index
            write_json(results_dir / "result.json", result)
            keep_launch_for_manual_check = False
            try:
                emit_status(
                    f"starting restart attempt {attempt_index} "
                    f"(successful restarts={result['successful_restarts']}, "
                    f"remaining={max(deadline - time.monotonic(), 0.0):.1f}s)"
                )
                session.start()
                _wait_for_camera_ready(session, harness, camera_name, emit_status)
                ok, snapshot, message = _wait_for_stable_streams(
                    session=session,
                    harness=harness,
                    topics=topics,
                    stable_seconds=stable_seconds,
                    timeout=stream_timeout,
                    max_gap_seconds=max_gap_seconds,
                    log_path=restart_log_path,
                    emit_status=emit_status,
                )
                attempt["topics"] = snapshot
                attempt["message"] = message
                attempt["stable_seconds"] = min(
                    (item.get("stable_seconds", 0.0) or 0.0 for item in snapshot),
                    default=0.0,
                )
                if not ok:
                    attempt["status"] = "warning"
                    result["status"] = "warning"
                    result["warning"] = (
                        f"attempt {attempt_index}: {message}; launch is left running "
                        "for manual confirmation"
                    )
                    keep_launch_for_manual_check = True
                    emit_status(
                        "[RESTART][WARNING] image stream did not become stable; "
                        "leaving launch running for manual confirmation"
                    )
                    write_json(results_dir / "result.json", result)
                    write_markdown(results_dir / "summary.md", _build_restart_summary(result))
                    while session.is_running():
                        harness.spin_once(0.2)
                        time.sleep(0.8)
                    break

                attempt["status"] = "passed"
                emit_status(
                    f"restart attempt {attempt_index} passed: {message}"
                )
            except KeyboardInterrupt:
                result["status"] = "interrupted"
                attempt["status"] = "interrupted"
                attempt["message"] = "interrupted by user"
                if keep_launch_for_manual_check:
                    keep_launch_for_manual_check = False
                    emit_status(
                        "restart test interrupted by user; stopping launch left for manual confirmation"
                    )
                else:
                    emit_status("restart test interrupted by user")
                break
            except Exception as exc:  # noqa: BLE001
                result["status"] = "failed"
                attempt["status"] = "failed"
                attempt["message"] = str(exc)
                emit_status(f"restart attempt {attempt_index} failed: {exc}")
                break
            finally:
                attempt["ended_at"] = datetime.now().isoformat(timespec="seconds")
                if not keep_launch_for_manual_check:
                    session.stop()

            if result["status"] != "passed":
                break
            if time.monotonic() >= deadline:
                emit_status("restart duration reached; no further launch restart will be started")
                break

            result["successful_restarts"] += 1
            emit_status(
                f"launch restart completed; total successful restarts="
                f"{result['successful_restarts']}"
            )
            time.sleep(min(float(args.restart_delay), max(deadline - time.monotonic(), 0.0)))
            write_json(results_dir / "result.json", result)

    write_json(results_dir / "result.json", result)
    write_markdown(results_dir / "summary.md", _build_restart_summary(result))
    if result["status"] == "passed":
        emit_status(
            f"restart test finished successfully, successful restarts="
            f"{result['successful_restarts']}"
        )
        return 0
    if result["status"] == "warning":
        return 2
    if result["status"] == "interrupted":
        return 130
    return 1


def parse_args():
    parser = argparse.ArgumentParser(description="Run Orbbec camera launch restart tests")
    parser.add_argument("--profile", default="", help="Optional profile name or YAML path")
    parser.add_argument("--launch-file", default="", help="Launch file to restart")
    parser.add_argument(
        "--performance-scenario",
        default="",
        help="Use launch args/topics from a performance scenario; requires --profile",
    )
    parser.add_argument("--camera-name", default=None)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--usb-port", default=None)
    parser.add_argument("--config-file-path", default=None)
    parser.add_argument("--driver-setup", default=None)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--launch-arg", action="append", default=[], help="Extra KEY=VALUE launch arg")
    parser.add_argument("--image-topic", action="append", default=[], help="Image topic to monitor, may be repeated")
    parser.add_argument("--duration", default="300", help="Total restart stress duration, supports 300, 15m, 2h")
    parser.add_argument("--stable-seconds", default="10", help="Required continuous image stream duration")
    parser.add_argument("--stream-timeout", default="60", help="Max wait time for stable stream per launch")
    parser.add_argument("--max-gap-seconds", default="1.5", help="Max allowed gap between received images")
    parser.add_argument("--restart-delay", type=float, default=2.0, help="Delay between stop and next start")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        return_code = run_restart_test(args)
    except KeyboardInterrupt:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] restart test interrupted by user", flush=True)
        return_code = 130
    sys.exit(return_code)


if __name__ == "__main__":
    main()
