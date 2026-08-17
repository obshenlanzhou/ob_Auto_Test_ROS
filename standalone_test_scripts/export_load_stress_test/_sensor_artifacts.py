from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


POINT_CLOUD_TYPES = {"sensor_msgs/msg/PointCloud2", "sensor_msgs/PointCloud2"}
IMU_TYPES = {"sensor_msgs/msg/Imu", "sensor_msgs/Imu"}
ARTIFACT_FILE_PATTERN = re.compile(
    r"^(?:image|point_cloud)_(\d+)\.(?:png|ply)$", re.IGNORECASE
)
POINT_FIELD_DTYPES = {
    1: ("i1", 1),
    2: ("u1", 1),
    3: ("i2", 2),
    4: ("u2", 2),
    5: ("i4", 4),
    6: ("u4", 4),
    7: ("f4", 4),
    8: ("f8", 8),
}


def normalize_topic(topic: str) -> str:
    return "/" + str(topic or "").strip().strip("/")


def camera_for_topic(topic: str, camera_names: Sequence[str]) -> Optional[str]:
    normalized = normalize_topic(topic)
    for camera_name in sorted(camera_names, key=len, reverse=True):
        prefix = normalize_topic(camera_name)
        if normalized.startswith(prefix + "/"):
            return camera_name.strip("/")
        if "/" not in camera_name.strip("/"):
            topic_parts = normalized.strip("/").split("/")
            if camera_name.strip("/") in topic_parts[:-1]:
                return camera_name.strip("/")
    return None


def expand_topic_templates(
    templates: Iterable[str], camera_names: Sequence[str]
) -> List[str]:
    expanded: List[str] = []
    for template in templates:
        text = str(template or "").strip()
        if not text:
            continue
        if "{camera}" in text or "${camera}" in text:
            for camera_name in camera_names:
                expanded.append(
                    normalize_topic(
                        text.replace("{camera}", camera_name).replace(
                            "${camera}", camera_name
                        )
                    )
                )
        else:
            expanded.append(normalize_topic(text))
    return sorted(set(expanded))


def _topic_has_type(type_names: Sequence[str], accepted: set[str]) -> bool:
    return any(type_name in accepted for type_name in type_names)


def discover_sensor_topics(
    *,
    harness: Any,
    camera_names: Sequence[str],
    point_cloud_topics: Sequence[str],
    imu_topics: Sequence[str],
    timeout: float,
    ensure_running: Optional[Callable[[], None]] = None,
    settle_seconds: float = 1.0,
) -> Tuple[List[str], List[str], Dict[str, str]]:
    explicit_point_clouds = sorted(set(map(normalize_topic, point_cloud_topics)))
    explicit_imus = sorted(set(map(normalize_topic, imu_topics)))
    deadline = time.monotonic() + max(float(timeout), 0.0)
    last_signature: Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]] = None
    unchanged_since: Optional[float] = None
    latest_point_clouds: List[str] = []
    latest_imus: List[str] = []
    latest_types: Dict[str, List[str]] = {}

    while True:
        if ensure_running is not None:
            ensure_running()
        topic_types = {
            normalize_topic(name): list(type_names)
            for name, type_names in harness.get_topic_names_and_types().items()
        }
        auto_point_clouds = sorted(
            name
            for name, type_names in topic_types.items()
            if camera_for_topic(name, camera_names) is not None
            and _topic_has_type(type_names, POINT_CLOUD_TYPES)
        )
        auto_imus = sorted(
            name
            for name, type_names in topic_types.items()
            if camera_for_topic(name, camera_names) is not None
            and _topic_has_type(type_names, IMU_TYPES)
        )
        latest_point_clouds = (
            explicit_point_clouds if explicit_point_clouds else auto_point_clouds
        )
        latest_imus = explicit_imus if explicit_imus else auto_imus
        latest_types = topic_types

        missing = [
            topic
            for topic in explicit_point_clouds
            if not _topic_has_type(topic_types.get(topic, []), POINT_CLOUD_TYPES)
        ] + [
            topic
            for topic in explicit_imus
            if not _topic_has_type(topic_types.get(topic, []), IMU_TYPES)
        ]
        signature = (tuple(latest_point_clouds), tuple(latest_imus))
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            unchanged_since = now
        if (
            not missing
            and unchanged_since is not None
            and now - unchanged_since >= settle_seconds
        ):
            break
        if now >= deadline:
            if missing:
                raise TimeoutError(
                    "configured sensor topic(s) were not advertised with the expected "
                    f"type within {timeout:.1f}s: {', '.join(missing)}"
                )
            break
        harness.spin_once(0.1)

    topic_cameras: Dict[str, str] = {}
    for topic in [*latest_point_clouds, *latest_imus]:
        camera_name = camera_for_topic(topic, camera_names)
        if camera_name is None:
            if topic in explicit_point_clouds or topic in explicit_imus:
                raise ValueError(
                    f"configured sensor topic is outside camera namespaces: {topic}"
                )
            continue
        topic_cameras[topic] = camera_name
    return latest_point_clouds, latest_imus, topic_cameras


def sensor_stream_name(topic: str, kind: str) -> str:
    parts = [part for part in normalize_topic(topic).split("/") if part]
    if kind == "point_cloud":
        if len(parts) >= 2 and parts[-2:] == ["depth", "points"]:
            return "point_cloud_depth"
        if len(parts) >= 2 and parts[-2:] == ["depth_registered", "points"]:
            return "point_cloud_registered"
        parent = parts[-2] if len(parts) > 1 else "points"
        return "point_cloud_" + _safe_path_part(parent)
    if len(parts) >= 2 and parts[-2:] == ["accel", "sample"]:
        return "imu_accel"
    if len(parts) >= 2 and parts[-2:] == ["gyro", "sample"]:
        return "imu_gyro"
    if len(parts) >= 2 and parts[-2:] == ["gyro_accel", "sample"]:
        return "imu_gyro_accel"
    parent = parts[-2] if len(parts) > 1 else "sample"
    return "imu_" + _safe_path_part(parent)


def _safe_path_part(value: str) -> str:
    text = str(value or "").strip().strip("/")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text) or "unknown"


class SensorArtifactPathSequence:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self._next_indices: Dict[Tuple[str, str], int] = {}

    def next_path(self, camera_name: str, topic: str, kind: str) -> Path:
        safe_camera = _safe_path_part(camera_name)
        stream_name = sensor_stream_name(topic, kind)
        key = (safe_camera, stream_name)
        stream_dir = self.output_root / safe_camera / stream_name
        stream_dir.mkdir(parents=True, exist_ok=True)
        next_index = self._next_indices.get(key)
        if next_index is None:
            indices = [
                int(match.group(1))
                for path in stream_dir.iterdir()
                if path.is_file()
                and (match := ARTIFACT_FILE_PATTERN.match(path.name))
            ]
            next_index = max(indices, default=0) + 1
        filename = (
            f"point_cloud_{next_index:04d}.ply"
            if kind == "point_cloud"
            else f"image_{next_index:04d}.png"
        )
        target = stream_dir / filename
        while target.exists():
            next_index += 1
            filename = (
                f"point_cloud_{next_index:04d}.ply"
                if kind == "point_cloud"
                else f"image_{next_index:04d}.png"
            )
            target = stream_dir / filename
        self._next_indices[key] = next_index + 1
        return target


def _field_map(message: Any) -> Dict[str, Any]:
    return {str(field.name).lower(): field for field in getattr(message, "fields", [])}


def _field_array(message: Any, field: Any, *, packed_bits: bool = False):
    import numpy as np

    datatype = int(getattr(field, "datatype", 0) or 0)
    if datatype not in POINT_FIELD_DTYPES:
        raise ValueError(
            f"unsupported PointField datatype {datatype} for {getattr(field, 'name', '')}"
        )
    dtype_code, size = POINT_FIELD_DTYPES[datatype]
    if packed_bits:
        if size != 4:
            raise ValueError("packed point color field must be 4 bytes")
        dtype_code = "u4"
    endian = ">" if bool(getattr(message, "is_bigendian", False)) else "<"
    dtype = np.dtype(endian + dtype_code)
    height = max(int(getattr(message, "height", 0) or 0), 1)
    width = int(getattr(message, "width", 0) or 0)
    point_step = int(getattr(message, "point_step", 0) or 0)
    row_step = int(getattr(message, "row_step", 0) or 0) or point_step * width
    offset = int(getattr(field, "offset", 0) or 0)
    if width <= 0 or point_step <= 0:
        raise ValueError("point cloud width and point_step must be positive")
    required_size = (height - 1) * row_step + (width - 1) * point_step + offset + size
    data = memoryview(getattr(message, "data", b"") or b"")
    if len(data) < required_size:
        raise ValueError(
            f"point cloud payload is truncated: {len(data)} < {required_size}"
        )
    return np.ndarray(
        shape=(height, width),
        dtype=dtype,
        buffer=data,
        offset=offset,
        strides=(row_step, point_step),
    )


def extract_point_cloud(message: Any, max_points: Optional[int] = 50000):
    import numpy as np

    fields = _field_map(message)
    missing = [name for name in ("x", "y", "z") if name not in fields]
    if missing:
        raise ValueError(f"point cloud is missing field(s): {', '.join(missing)}")
    x = _field_array(message, fields["x"]).reshape(-1).astype(np.float64)
    y = _field_array(message, fields["y"]).reshape(-1).astype(np.float64)
    z = _field_array(message, fields["z"]).reshape(-1).astype(np.float64)
    finite_indices = np.flatnonzero(np.isfinite(x) & np.isfinite(y) & np.isfinite(z))
    valid_count = int(finite_indices.size)
    if valid_count <= 0:
        raise ValueError("point cloud has no finite XYZ points")
    if max_points is not None and valid_count > max_points:
        selection = np.linspace(0, valid_count - 1, max_points, dtype=np.int64)
        finite_indices = finite_indices[selection]
    points = np.column_stack(
        (x[finite_indices], y[finite_indices], z[finite_indices])
    )

    colors = None
    color_field = fields.get("rgb") or fields.get("rgba")
    if color_field is not None:
        packed = _field_array(message, color_field, packed_bits=True).reshape(-1)
        values = packed[finite_indices].astype(np.uint32)
        colors = np.column_stack(
            (
                ((values >> 16) & 0xFF),
                ((values >> 8) & 0xFF),
                (values & 0xFF),
            )
        ).astype(np.uint8)
    return points, colors, valid_count


def write_point_cloud_ply(path: Path, *, points: Any, colors: Any) -> None:
    import numpy as np

    xyz = np.asarray(points, dtype="<f4")
    if xyz.ndim != 2 or xyz.shape[1] != 3:
        raise ValueError("point array must have shape (N, 3)")
    has_color = colors is not None
    dtype_fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if has_color:
        rgb = np.asarray(colors, dtype=np.uint8)
        if rgb.shape != xyz.shape:
            raise ValueError("point color array must have shape (N, 3)")
        dtype_fields.extend([("red", "u1"), ("green", "u1"), ("blue", "u1")])
    vertices = np.empty(len(xyz), dtype=np.dtype(dtype_fields))
    vertices["x"], vertices["y"], vertices["z"] = xyz.T
    if has_color:
        vertices["red"], vertices["green"], vertices["blue"] = rgb.T
    properties = ["property float x", "property float y", "property float z"]
    if has_color:
        properties.extend(
            ["property uchar red", "property uchar green", "property uchar blue"]
        )
    header = "\n".join(
        [
            "ply",
            "format binary_little_endian 1.0",
            "comment generated from ROS PointCloud2 finite XYZ points",
            f"element vertex {len(vertices)}",
            *properties,
            "end_header",
            "",
        ]
    ).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(header)
        stream.write(vertices.tobytes())


def _stamp_nonzero(message: Any) -> bool:
    header = getattr(message, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return False
    seconds = getattr(stamp, "sec", getattr(stamp, "secs", 0))
    nanoseconds = getattr(stamp, "nanosec", getattr(stamp, "nsecs", 0))
    return bool(seconds or nanoseconds)


def imu_mode(topic: str) -> str:
    normalized = normalize_topic(topic)
    if "/gyro_accel/" in normalized:
        return "gyro_accel"
    if "/accel/" in normalized:
        return "accel"
    if "/gyro/" in normalized:
        return "gyro"
    return "gyro_accel"


def extract_imu_sample(message: Any, topic: str, received_at: float) -> Dict[str, Any]:
    import numpy as np

    linear = getattr(message, "linear_acceleration", None)
    angular = getattr(message, "angular_velocity", None)
    values = {
        "linear": (
            float(getattr(linear, "x", 0.0)),
            float(getattr(linear, "y", 0.0)),
            float(getattr(linear, "z", 0.0)),
        ),
        "angular": (
            float(getattr(angular, "x", 0.0)),
            float(getattr(angular, "y", 0.0)),
            float(getattr(angular, "z", 0.0)),
        ),
    }
    if not _stamp_nonzero(message):
        raise ValueError("IMU message header stamp is zero")
    mode = imu_mode(topic)
    relevant = values["linear"] if mode == "accel" else values["angular"]
    if mode == "gyro_accel":
        relevant = (*values["linear"], *values["angular"])
    if not np.isfinite(np.asarray(relevant, dtype=float)).all():
        raise ValueError("IMU message contains non-finite values")
    return {"received_at": received_at, **values}


def _draw_imu_panel(canvas, samples, value_key, rect, title, unit):
    import cv2
    import numpy as np

    left, top, width, height = rect
    panel = canvas[top : top + height, left : left + width]
    panel[:] = (28, 31, 36)
    margin_left, margin_right, margin_top, margin_bottom = 70, 20, 35, 45
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    times = np.asarray([row["received_at"] for row in samples], dtype=float)
    times -= times[0]
    values = np.asarray([row[value_key] for row in samples], dtype=float)
    value_min = float(np.min(values))
    value_max = float(np.max(values))
    if value_max <= value_min:
        value_min -= 1.0
        value_max += 1.0
    pad = max((value_max - value_min) * 0.08, 1e-6)
    value_min -= pad
    value_max += pad
    x_pixels = margin_left + (
        times / max(float(times[-1]), 1e-9) * max(plot_width - 1, 1)
    ).astype(int)
    palette = ((80, 100, 255), (90, 220, 120), (255, 170, 70))
    for axis in range(3):
        y_pixels = margin_top + (
            (value_max - values[:, axis])
            / max(value_max - value_min, 1e-12)
            * max(plot_height - 1, 1)
        ).astype(int)
        polyline = np.column_stack((x_pixels, y_pixels)).astype(np.int32)
        cv2.polylines(panel, [polyline], False, palette[axis], 2, cv2.LINE_AA)
        cv2.putText(panel, "XYZ"[axis], (margin_left + axis * 42, height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, palette[axis], 1, cv2.LINE_AA)
    cv2.rectangle(panel, (margin_left, margin_top), (margin_left + plot_width, margin_top + plot_height), (110, 116, 124), 1)
    cv2.putText(panel, f"{title} ({unit})", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (235, 238, 242), 1, cv2.LINE_AA)
    cv2.putText(panel, f"{value_max:.4g}", (5, margin_top + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 185, 192), 1, cv2.LINE_AA)
    cv2.putText(panel, f"{value_min:.4g}", (5, margin_top + plot_height), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 185, 192), 1, cv2.LINE_AA)
    cv2.putText(panel, f"{times[-1]:.2f}s", (margin_left + plot_width - 45, height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 185, 192), 1, cv2.LINE_AA)


def render_imu_png(path: Path, *, topic: str, samples: Sequence[Dict[str, Any]]) -> None:
    import cv2
    import numpy as np

    mode = imu_mode(topic)
    duration = max(samples[-1]["received_at"] - samples[0]["received_at"], 0.0)
    frequency = (len(samples) - 1) / duration if duration > 0 and len(samples) > 1 else 0.0
    panel_count = 2 if mode == "gyro_accel" else 1
    canvas_height = 130 + panel_count * 330
    canvas = np.full((canvas_height, 1000, 3), (20, 22, 26), dtype=np.uint8)
    cv2.putText(canvas, topic, (24, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (245, 247, 250), 2, cv2.LINE_AA)
    cv2.putText(
        canvas,
        f"samples: {len(samples)}; duration: {duration:.2f}s; estimated rate: {frequency:.2f} Hz",
        (24, 62),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (185, 191, 199),
        1,
        cv2.LINE_AA,
    )
    if mode in {"accel", "gyro_accel"}:
        _draw_imu_panel(canvas, samples, "linear", (10, 85, 980, 315), "Linear acceleration", "m/s^2")
    if mode == "gyro":
        _draw_imu_panel(canvas, samples, "angular", (10, 85, 980, 315), "Angular velocity", "rad/s")
    elif mode == "gyro_accel":
        _draw_imu_panel(canvas, samples, "angular", (10, 415, 980, 315), "Angular velocity", "rad/s")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), canvas, [int(cv2.IMWRITE_PNG_COMPRESSION), 1]):
        raise RuntimeError(f"failed to write IMU PNG: {path}")


class SensorCaptureMonitor:
    def __init__(
        self,
        *,
        harness: Any,
        point_cloud_topics: Sequence[str],
        imu_topics: Sequence[str],
        topic_cameras: Dict[str, str],
        output_root: Path,
        save_count: int,
        imu_window_seconds: float = 2.0,
        imu_min_samples: int = 10,
        max_points: int = 50000,
    ) -> None:
        self.harness = harness
        self.topic_cameras = topic_cameras
        self.save_count = max(int(save_count), 0)
        self.required_outputs = max(self.save_count, 1)
        self.imu_window_seconds = float(imu_window_seconds)
        self.imu_min_samples = int(imu_min_samples)
        self.max_points = int(max_points)
        self.paths = SensorArtifactPathSequence(output_root)
        self.subscriptions: List[Any] = []
        self.state: Dict[str, Dict[str, Any]] = {}

        for topic in point_cloud_topics:
            self.state[topic] = {
                "kind": "point_cloud",
                "message_count": 0,
                "valid_count": 0,
                "saved_point_count": 0,
                "files": [],
                "error": "",
            }
            self.subscriptions.append(
                harness.create_sensor_subscription(
                    topic,
                    "point_cloud",
                    lambda message, topic_name=topic: self._on_point_cloud(
                        topic_name, message
                    ),
                )
            )
        for topic in imu_topics:
            self.state[topic] = {
                "kind": "imu",
                "mode": imu_mode(topic),
                "message_count": 0,
                "valid_sample_count": 0,
                "completed_windows": 0,
                "current_window": [],
                "files": [],
                "error": "",
            }
            self.subscriptions.append(
                harness.create_sensor_subscription(
                    topic,
                    "imu",
                    lambda message, topic_name=topic: self._on_imu(
                        topic_name, message
                    ),
                )
            )

    def _on_point_cloud(self, topic: str, message: Any) -> None:
        item = self.state[topic]
        item["message_count"] += 1
        if item["valid_count"] >= self.required_outputs or item["error"]:
            return
        try:
            points, colors, valid_count = extract_point_cloud(
                message,
                max_points=None if self.save_count > 0 else self.max_points,
            )
            item["valid_count"] += 1
            item["finite_point_count"] = valid_count
            item["data_size"] = len(getattr(message, "data", b"") or b"")
            item["width"] = int(getattr(message, "width", 0) or 0)
            item["height"] = int(getattr(message, "height", 0) or 0)
            if self.save_count > 0:
                camera_name = self.topic_cameras.get(topic, "unknown_camera")
                path = self.paths.next_path(camera_name, topic, "point_cloud")
                write_point_cloud_ply(
                    path,
                    points=points,
                    colors=colors,
                )
                item["files"].append(str(path))
                item["saved_point_count"] = len(points)
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)

    def _on_imu(self, topic: str, message: Any) -> None:
        item = self.state[topic]
        item["message_count"] += 1
        if item["completed_windows"] >= self.required_outputs or item["error"]:
            return
        try:
            sample = extract_imu_sample(message, topic, time.monotonic())
            item["valid_sample_count"] += 1
            window = item["current_window"]
            window.append(sample)
            if (
                len(window) >= self.imu_min_samples
                and window[-1]["received_at"] - window[0]["received_at"]
                >= self.imu_window_seconds
            ):
                item["completed_windows"] += 1
                if self.save_count > 0:
                    camera_name = self.topic_cameras.get(topic, "unknown_camera")
                    path = self.paths.next_path(camera_name, topic, "imu")
                    render_imu_png(path, topic=topic, samples=window)
                    item["files"].append(str(path))
                item["last_window_sample_count"] = len(window)
                item["last_window_duration"] = (
                    window[-1]["received_at"] - window[0]["received_at"]
                )
                item["current_window"] = []
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)

    def first_error(self) -> str:
        for topic, item in self.state.items():
            if item.get("error"):
                return f"{topic}: {item['error']}"
        return ""

    def complete(self) -> bool:
        for item in self.state.values():
            if item["kind"] == "point_cloud":
                if item["valid_count"] < self.required_outputs:
                    return False
            elif item["completed_windows"] < self.required_outputs:
                return False
        return True

    def snapshot(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for topic, item in self.state.items():
            row = {
                "name": topic,
                "topic": topic,
                "camera": self.topic_cameras.get(topic, ""),
                "kind": item["kind"],
                "message_count": item["message_count"],
                "files": list(item["files"]),
                "saved_count": len(item["files"]),
                "expected_count": self.save_count,
                "error": item.get("error", ""),
            }
            if item["kind"] == "point_cloud":
                row.update(
                    {
                        "valid_message_count": item["valid_count"],
                        "finite_point_count": item.get("finite_point_count", 0),
                        "saved_point_count": item.get("saved_point_count", 0),
                        "width": item.get("width", 0),
                        "height": item.get("height", 0),
                        "data_size": item.get("data_size", 0),
                    }
                )
            else:
                row.update(
                    {
                        "mode": item["mode"],
                        "valid_sample_count": item["valid_sample_count"],
                        "completed_windows": item["completed_windows"],
                        "window_seconds": item.get("last_window_duration", 0.0),
                        "window_sample_count": item.get(
                            "last_window_sample_count", 0
                        ),
                    }
                )
            rows.append(row)
        return rows

    def close(self) -> None:
        for subscription in list(self.subscriptions):
            self.harness.destroy_subscription(subscription)
        self.subscriptions = []


def capture_sensor_artifacts(
    *,
    harness: Any,
    point_cloud_topics: Sequence[str],
    imu_topics: Sequence[str],
    topic_cameras: Dict[str, str],
    output_root: Path,
    save_count: int,
    timeout: float,
    ensure_running: Optional[Callable[[], None]] = None,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    if not point_cloud_topics and not imu_topics:
        return True, [], "no point cloud or IMU topics selected"
    monitor = SensorCaptureMonitor(
        harness=harness,
        point_cloud_topics=point_cloud_topics,
        imu_topics=imu_topics,
        topic_cameras=topic_cameras,
        output_root=output_root,
        save_count=save_count,
    )
    deadline = time.monotonic() + max(float(timeout), 0.0)
    try:
        while time.monotonic() < deadline:
            if ensure_running is not None:
                ensure_running()
            harness.spin_once(0.1)
            if monitor.first_error():
                return False, monitor.snapshot(), monitor.first_error()
            if monitor.complete():
                snapshot = monitor.snapshot()
                files = sum(len(row["files"]) for row in snapshot)
                return (
                    True,
                    snapshot,
                    f"validated {len(snapshot)} sensor stream(s) and saved {files} artifact file(s)",
                )
        return (
            False,
            monitor.snapshot(),
            f"point cloud/IMU streams were not complete within {timeout:.1f}s",
        )
    finally:
        monitor.close()
