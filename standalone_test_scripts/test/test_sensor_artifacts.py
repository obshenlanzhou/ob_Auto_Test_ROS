from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPERS = [
    ROOT / directory / "_sensor_artifacts.py"
    for directory in (
        "export_load_stress_test",
        "launch_param_load_stress",
        "preset_upgrade_stress_test",
        "stream_toggle_stress_test",
        "launch_restart_stream_check",
    )
]


def load_helper():
    path = HELPERS[0]
    spec = importlib.util.spec_from_file_location("standalone_sensor_artifacts", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_script(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def point_cloud_message(*, bigendian=False, row_padding=0):
    byte_order = ">" if bigendian else "<"
    points = [
        (0.0, 0.0, 1.0, 0x00FF0000),
        (1.0, 0.0, 2.0, 0x0000FF00),
        (0.0, 1.0, 3.0, 0x000000FF),
        (float("nan"), 1.0, 4.0, 0x00FFFFFF),
    ]
    payload = b"".join(struct.pack(byte_order + "fffI", *point) for point in points)
    payload += b"\0" * row_padding
    fields = [
        SimpleNamespace(name="x", offset=0, datatype=7),
        SimpleNamespace(name="y", offset=4, datatype=7),
        SimpleNamespace(name="z", offset=8, datatype=7),
        SimpleNamespace(name="rgb", offset=12, datatype=6),
    ]
    return SimpleNamespace(
        fields=fields,
        is_bigendian=bigendian,
        height=1,
        width=4,
        point_step=16,
        row_step=64 + row_padding,
        data=payload,
    )


def imu_message():
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=1, nanosec=2)),
        linear_acceleration=SimpleNamespace(x=0.1, y=0.2, z=9.8),
        angular_velocity=SimpleNamespace(x=0.01, y=0.02, z=0.03),
    )


class FakeHarness:
    def __init__(self, topic_types=None):
        self.topic_types = topic_types or {}
        self.callbacks = {}

    def get_topic_names_and_types(self):
        return self.topic_types

    def spin_once(self, _timeout):
        return None

    def create_sensor_subscription(self, topic, kind, callback):
        self.callbacks[topic] = (kind, callback)
        return topic

    def destroy_subscription(self, subscription):
        self.callbacks.pop(subscription, None)


def test_local_helpers_remain_identical_for_independent_delivery():
    expected = HELPERS[0].read_bytes()
    assert all(path.read_bytes() == expected for path in HELPERS)


def test_topic_template_expansion_and_ros1_ros2_discovery():
    module = load_helper()
    assert (
        module.camera_for_topic(
            "/rig/camera_01/depth/points", ["camera_01"]
        )
        == "camera_01"
    )
    assert module.expand_topic_templates(
        ["/{camera}/depth/points", "/camera_01/gyro/sample"],
        ["camera_01", "camera_02"],
    ) == [
        "/camera_01/depth/points",
        "/camera_01/gyro/sample",
        "/camera_02/depth/points",
    ]
    harness = FakeHarness(
        {
            "/camera_01/depth/points": ["sensor_msgs/msg/PointCloud2"],
            "/camera_01/gyro/sample": ["sensor_msgs/Imu"],
            "/camera_02/depth/points": ["sensor_msgs/PointCloud2"],
            "/other/gyro/sample": ["sensor_msgs/msg/Imu"],
        }
    )
    point_clouds, imus, cameras = module.discover_sensor_topics(
        harness=harness,
        camera_names=["camera_01", "camera_02"],
        point_cloud_topics=[],
        imu_topics=[],
        timeout=0,
        settle_seconds=0,
    )
    assert point_clouds == [
        "/camera_01/depth/points",
        "/camera_02/depth/points",
    ]
    assert imus == ["/camera_01/gyro/sample"]
    assert cameras["/camera_02/depth/points"] == "camera_02"


def test_explicit_sensor_topic_is_required():
    module = load_helper()
    with pytest.raises(TimeoutError, match="configured sensor topic"):
        module.discover_sensor_topics(
            harness=FakeHarness(),
            camera_names=["camera"],
            point_cloud_topics=["/camera/depth/points"],
            imu_topics=[],
            timeout=0,
            settle_seconds=0,
        )


@pytest.mark.parametrize("bigendian", [False, True])
def test_point_cloud_parser_handles_endianness_and_packed_rgb(bigendian):
    np = pytest.importorskip("numpy")
    module = load_helper()
    points, colors, valid_count = module.extract_point_cloud(
        point_cloud_message(bigendian=bigendian), max_points=50_000
    )
    assert valid_count == 3
    assert points.shape == (3, 3)
    assert np.allclose(points[:, 2], [1.0, 2.0, 3.0])
    assert colors.tolist() == [[255, 0, 0], [0, 255, 0], [0, 0, 255]]


def test_point_cloud_ply_writer_and_imu_png_renderer(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    module = load_helper()
    points, colors, _valid_count = module.extract_point_cloud(
        point_cloud_message(), max_points=None
    )
    point_cloud_path = tmp_path / "point_cloud.ply"
    module.write_point_cloud_ply(
        point_cloud_path,
        points=points,
        colors=colors,
    )
    header, payload = point_cloud_path.read_bytes().split(b"end_header\n", 1)
    assert b"format binary_little_endian 1.0" in header
    assert b"element vertex 3" in header
    vertices = np.frombuffer(
        payload,
        dtype=np.dtype(
            [
                ("x", "<f4"),
                ("y", "<f4"),
                ("z", "<f4"),
                ("red", "u1"),
                ("green", "u1"),
                ("blue", "u1"),
            ]
        ),
    )
    assert np.allclose(vertices["z"], [1.0, 2.0, 3.0])
    assert vertices[["red", "green", "blue"]].tolist() == [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
    ]
    samples = [
        {
            "received_at": index * 0.25,
            "linear": (index * 0.1, index * 0.2, 9.8),
            "angular": (index * 0.01, index * 0.02, index * 0.03),
        }
        for index in range(10)
    ]
    imu_path = tmp_path / "imu.png"
    module.render_imu_png(
        imu_path,
        topic="/camera/gyro_accel/sample",
        samples=samples,
    )
    assert cv2.imread(str(imu_path)).shape == (790, 1000, 3)


def test_sensor_monitor_validates_when_saving_is_disabled(monkeypatch, tmp_path):
    module = load_helper()
    harness = FakeHarness()
    monitor = module.SensorCaptureMonitor(
        harness=harness,
        point_cloud_topics=["/camera/depth/points"],
        imu_topics=["/camera/accel/sample"],
        topic_cameras={
            "/camera/depth/points": "camera",
            "/camera/accel/sample": "camera",
        },
        output_root=tmp_path,
        save_count=0,
    )
    times = iter(index * 0.25 for index in range(10))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    harness.callbacks["/camera/depth/points"][1](point_cloud_message())
    for _ in range(10):
        harness.callbacks["/camera/accel/sample"][1](imu_message())
    monitor.wait_for_pending_output(1.0)
    assert monitor.complete()
    snapshot = {row["topic"]: row for row in monitor.snapshot()}
    assert snapshot["/camera/depth/points"]["finite_point_count"] == 3
    assert snapshot["/camera/accel/sample"]["completed_windows"] == 1
    assert not list(tmp_path.rglob("*.png"))
    assert not list(tmp_path.rglob("*.ply"))
    monitor.close()


def test_sensor_point_cloud_processing_runs_outside_ros_callback(monkeypatch, tmp_path):
    module = load_helper()
    harness = FakeHarness()
    monitor = module.SensorCaptureMonitor(
        harness=harness,
        point_cloud_topics=["/camera/depth/points"],
        imu_topics=[],
        topic_cameras={"/camera/depth/points": "camera"},
        output_root=tmp_path,
        save_count=0,
    )
    worker_started = module.threading.Event()
    release_worker = module.threading.Event()
    original_extract = module.extract_point_cloud

    def blocking_extract(message, max_points):
        worker_started.set()
        assert release_worker.wait(1.0)
        return original_extract(message, max_points=max_points)

    monkeypatch.setattr(module, "extract_point_cloud", blocking_extract)
    callback = harness.callbacks["/camera/depth/points"][1]
    caller = module.threading.Thread(target=callback, args=(point_cloud_message(),))
    caller.start()
    assert worker_started.wait(1.0)
    caller.join(0.1)
    assert not caller.is_alive()
    assert not monitor.complete()

    release_worker.set()
    monitor.wait_for_pending_output(1.0)
    assert monitor.complete()
    monitor.close()


def test_persistent_sensor_monitor_reuses_subscriptions_across_captures(tmp_path):
    module = load_helper()

    class PublishingHarness(FakeHarness):
        def __init__(self):
            super().__init__()
            self.create_count = 0
            self.destroy_count = 0

        def create_sensor_subscription(self, topic, kind, callback):
            self.create_count += 1
            return super().create_sensor_subscription(topic, kind, callback)

        def destroy_subscription(self, subscription):
            self.destroy_count += 1
            super().destroy_subscription(subscription)

        def spin_once(self, _timeout):
            self.callbacks["/camera/depth/points"][1](point_cloud_message())

    harness = PublishingHarness()
    monitor = module.SensorCaptureMonitor(
        harness=harness,
        point_cloud_topics=["/camera/depth/points"],
        imu_topics=[],
        topic_cameras={"/camera/depth/points": "camera"},
        output_root=tmp_path,
        save_count=0,
        active=False,
    )

    snapshots = []
    for _ in range(2):
        ok, snapshot, _message = module.capture_sensor_artifacts(
            harness=harness,
            point_cloud_topics=["/camera/depth/points"],
            imu_topics=[],
            topic_cameras={"/camera/depth/points": "camera"},
            output_root=tmp_path,
            save_count=0,
            timeout=1,
            monitor=monitor,
        )
        assert ok
        snapshots.append(snapshot)

    assert [snapshot[0]["message_count"] for snapshot in snapshots] == [1, 1]
    assert harness.create_count == 1
    assert harness.destroy_count == 0
    assert len(harness.callbacks) == 1

    monitor.close()
    assert harness.destroy_count == 1
    assert not harness.callbacks


def test_sensor_paths_use_stable_stream_names_and_continue_indices(tmp_path):
    module = load_helper()
    existing = tmp_path / "camera" / "point_cloud_depth" / "image_0003.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    sequence = module.SensorArtifactPathSequence(tmp_path)
    assert sequence.next_path(
        "camera", "/camera/depth/points", "point_cloud"
    ) == tmp_path / "camera" / "point_cloud_depth" / "point_cloud_0004.ply"
    assert sequence.next_path(
        "camera", "/camera/gyro_accel/sample", "imu"
    ) == tmp_path / "camera" / "imu_gyro_accel" / "image_0001.png"


def test_launch_restart_image_paths_are_per_camera_stream_and_non_overwriting(
    tmp_path,
):
    module = load_script(
        ROOT
        / "launch_restart_stream_check"
        / "launch_restart_stream_check.py",
        "launch_restart_stream_check_for_sensor_test",
    )
    existing = tmp_path / "camera_01" / "depth" / "image_0003.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    sequence = module.ImagePathSequence(tmp_path)
    assert sequence.next_path(
        "/camera_01/depth/image_raw", ".png"
    ) == tmp_path / "camera_01" / "depth" / "image_0004.png"
    assert sequence.next_path(
        "/camera_02/color/image_raw", ".png"
    ) == tmp_path / "camera_02" / "color" / "image_0001.png"
