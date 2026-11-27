import csv

import pytest

from orbbec_camera_auto_test.core import transport_config
from orbbec_camera_auto_test.runners.transport import _read_configured_gpu_summary


def test_cross_process_reuses_an_existing_multi_camera_launch(tmp_path, monkeypatch):
    launch = tmp_path / "multi_camera.launch.py"
    launch.touch()
    plan = tmp_path / "cameras.yaml"
    plan.write_text(
        "launch: multi_camera.launch.py\ncameras:\n"
        "  - {name: front, streams: [color, depth]}\n"
        "  - {name: rear, time_domain: device, streams: {points: /custom/rear/cloud}}\n"
    )
    monkeypatch.setattr(transport_config, "camera_launch_options", lambda *_: {})
    launches, streams = transport_config.prepare_transport_config(plan, tmp_path, {}, "NONE", False)
    assert launches == [{"path": str(launch), "arguments": {}}]
    assert [(stream["camera_name"], stream["topic"], stream["kind"], stream["host_clock"])
            for stream in streams] == [
        ("front", "/front/color/image_raw", "image", True),
        ("front", "/front/depth/image_raw", "image", True),
        ("rear", "/custom/rear/cloud", "points", False),
    ]
    with pytest.raises(ValueError, match="shared|component_container_name"):
        transport_config.prepare_transport_config(plan, tmp_path, {}, "NITROS_BUFFER", False)


@pytest.mark.parametrize(("backend", "suffix"), [
    ("NITROS_GXF", "/nitros_image"),
    ("NITROS_BUFFER", ""),
    ("CUDA_BUFFER", ""),
])
def test_individual_launches_set_devices_and_pair_topics(tmp_path, monkeypatch, backend, suffix):
    launch = tmp_path / "camera.launch.py"
    launch.touch()
    plan = tmp_path / "cameras.yaml"
    plan.write_text(
        "cameras:\n"
        "  - {name: front, launch: camera.launch.py, arguments: {usb_port: '2-1'}, streams: [color, points]}\n"
        "  - {name: rear, launch: camera.launch.py, arguments: {serial_number: '002'}, streams: [color]}\n"
    )
    calls = []

    def inspect_launch(path, _env):
        calls.append(path)
        return dict.fromkeys((
            "component_container_name", "camera_name", "serial_number", "usb_port",
            "device_num", "time_domain", "enable_gpu_transport", "use_intra_process_comms",
        ))

    monkeypatch.setattr(transport_config, "camera_launch_options", inspect_launch)
    launches, streams = transport_config.prepare_transport_config(plan, tmp_path, {}, backend, False)
    assert calls == [launch]
    assert launches[0]["arguments"] == {
        "camera_name": "front", "usb_port": "2-1", "device_num": "2",
        "enable_gpu_transport": "true", "use_intra_process_comms": "false",
        "time_domain": "global",
    }
    assert launches[1]["arguments"]["serial_number"] == "002"
    assert streams[0]["gpu_topic"] == "/front/color/image_raw" + suffix
    assert streams[1]["gpu_topic"] == "/front/depth/points" + (
        "/nitros_point_cloud" if backend.startswith("NITROS") else ""
    )
    assert streams[2]["gpu_topic"] == "/rear/color/image_raw" + suffix


def test_gpu_result_pairs_each_camera_without_mixing_same_named_streams(tmp_path):
    summary = tmp_path / "summary.csv"
    columns = (
        "phase", "elapsed_seconds", "camera_name", "stream", "topic", "backend",
        "samples", "complete", "avg_fps", "current_fps", "latency_avg_ms", "latency_p95_ms",
        "longest_gap_sec", "invalid_stamps", "delivery_mismatches", "read_errors",
        "cpu_fallback_frames", "system_cpu_pct", "system_gpu_pct",
    )
    with summary.open("w", newline="") as file:
        rows = csv.writer(file)
        rows.writerow(columns)
        for phase, backend in (("CPU (DDS)", "cpu"), ("CUDA Buffer", "CUDA_BUFFER")):
            for camera in ("front", "rear"):
                rows.writerow((phase, 2.0, camera, "color", f"/{camera}/color/image_raw", backend,
                               60, "true", 30, 25, 10, 12, 0.02, 0, 0, 0, 0, 45, 32))
    result = _read_configured_gpu_summary(summary, "CUDA_BUFFER", 2, final=True)
    assert [(row["camera_name"], row["phase"]) for row in result["rows"]] == [
        ("front", "CPU (DDS)"), ("front", "CUDA Buffer"),
        ("rear", "CPU (DDS)"), ("rear", "CUDA Buffer"),
    ]
    assert result["system"]["CUDA Buffer"] == {"cpu_percent": 45, "gpu_percent": 32}
    assert all(row["fps"] == 30 and row["latency_p95_ms"] == 12 for row in result["rows"])


def test_nitros_accepts_an_explicit_gpu_topic_for_a_remapped_stream(tmp_path, monkeypatch):
    launch = tmp_path / "composed.launch.py"
    launch.touch()
    plan = tmp_path / "cameras.yaml"
    plan.write_text(
        "launch: composed.launch.py\ncameras:\n"
        "  - name: camera_01\n"
        "    streams: {color: /rig/image}\n"
        "    gpu_topics: {color: /rig/gpu/image}\n"
    )
    monkeypatch.setattr(transport_config, "camera_launch_options", lambda *_: dict.fromkeys((
        "component_container_name", "use_intra_process_comms", "enable_gpu_transport",
    )))
    _, streams = transport_config.prepare_transport_config(plan, tmp_path, {}, "NITROS_BUFFER", False)
    assert streams[0]["topic"] == "/rig/image"
    assert streams[0]["gpu_topic"] == "/rig/gpu/image"
