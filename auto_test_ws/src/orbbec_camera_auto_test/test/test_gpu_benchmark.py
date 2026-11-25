import pytest

from orbbec_camera_auto_test.core.gpu_benchmark import (
    GpuBenchmarkProgress, gpu_benchmark_capability, read_gpu_summary,
)
from orbbec_camera_auto_test.core.reporter import build_performance_summary


@pytest.mark.parametrize(("backend", "launch_name"), [
    ("NITROS_GXF", "camera_nitros_subscriber.launch.py"),
    ("NITROS_BUFFER", "camera_nitros_subscriber.launch.py"),
    ("CUDA_BUFFER", "camera_cuda_buffer_subscriber.launch.py"),
])
def test_gpu_benchmark_resolves_installed_example(tmp_path, backend, launch_name):
    camera_share = tmp_path / "share" / "orbbec_camera"
    marker = camera_share.parent / "ament_index/resource_index/orbbec_gpu_backend/orbbec_camera"
    marker.parent.mkdir(parents=True)
    marker.write_text(backend)
    arguments = {"enable_gpu_transport": "true", "component_container_name": ""}
    assert gpu_benchmark_capability(camera_share, arguments) == (
        backend, None, f"The selected driver does not install {launch_name}",
    )

    standalone_launch = camera_share / "launch" / launch_name
    standalone_launch.parent.mkdir(parents=True)
    standalone_launch.touch()
    assert gpu_benchmark_capability(camera_share, arguments) == (backend, standalone_launch, "")

    example_launch = camera_share / "examples/gpu_subscribers" / launch_name
    example_launch.parent.mkdir(parents=True)
    example_launch.touch()
    assert gpu_benchmark_capability(camera_share, arguments) == (backend, example_launch, "")
    standalone_launch.unlink()
    assert gpu_benchmark_capability(camera_share, arguments) == (backend, example_launch, "")


def test_nitros_log_reports_gpu_backend_only_on_received_samples():
    progress = GpuBenchmarkProgress("NITROS_GXF", 10)
    progress.consume_line("Starting NITROS (IPC) phase, warmup=30 frames per stream, duration=10 s")
    assert progress.snapshot()["rows"] == []
    progress.consume_line(
        "[NITROS (IPC)/depth] Sampling 1.0/10 s, samples=30, FPS=30.00, LATENCY_avg=32.4 ms"
    )
    row = progress.snapshot()["rows"][0]
    assert row["backend"] == "NITROS_GXF"
    assert row["latency_ms"] == 32.4
    assert "cpu_fallback_frames" not in row


def test_cuda_log_tracks_fallbacks_separately_from_cuda_samples():
    progress = GpuBenchmarkProgress("CUDA_BUFFER", 10)
    progress.consume_line("[depth] Requested cuda, received cpu. Valid samples: 0, CPU fallback: 4")
    assert progress.snapshot()["rows"][0]["backend"] == "cpu"
    progress.consume_line(
        "[cuda/depth] Sample 1 elapsed=0.000/10 s backend=cuda fps=n/a publish_to_gpu_ms=n/a "
        "callback_to_gpu_ms=0.321 cpu_percent=5.000 gpu_percent=0.000"
    )
    row = progress.snapshot()["rows"][0]
    assert row["backend"] == "cuda" and row["cpu_fallback_frames"] == 4
    assert row["latency_ms"] is None and row["callback_to_gpu_ms"] == 0.321


def test_cuda_summary_preserves_two_distinct_latency_metrics(tmp_path):
    csv = tmp_path / "results.csv"
    csv.write_text(
        "stream,phase,status,received_frames,valid_frames,cpu_fallback_frames,read_errors,"
        "metric,values,mean,min,p95,max,stddev\n"
        "depth,cuda,complete,33,30,3,0,publish_to_gpu_ms,30,2.0,1.0,3.0,4.0,0.1\n"
        "depth,cuda,complete,33,30,3,0,callback_to_gpu_ms,30,0.2,0.1,0.3,0.4,0.01\n"
        "system,cuda,complete,,,,,gpu_percent,10,12.0,10.0,12.5,13.0,0.1\n",
        encoding="utf-8",
    )
    rows, system = read_gpu_summary(csv, "CUDA_BUFFER")
    assert rows["cuda", "depth"]["latency_basis"] == "publish_to_gpu"
    assert rows["cuda", "depth"]["latency_p95_ms"] == 3.0
    assert rows["cuda", "depth"]["callback_to_gpu_p95_ms"] == 0.3
    assert rows["cuda", "depth"]["cpu_fallback_frames"] == 3
    assert system["cuda"]["gpu_percent"] == 12.0
    markdown = build_performance_summary({
        "profile_name": "generic_performance",
        "transport_receiver": "gpu",
        "gpu_backend": "CUDA_BUFFER",
        "gpu_transport_summary": list(rows.values()),
        "gpu_system_summary": system,
    })
    assert "| depth | N/A | N/A | CUDA Buffer | cuda | n/a | n/a | n/a | n/a | 12.000 |" in markdown


@pytest.mark.parametrize(("latency_text", "basis", "latency", "callback"), [
    ("LATENCY_avg=42.982 ms", "frame_to_callback", 42.982, None),
    ("LATENCY_avg=n/a ms", "frame_to_callback", None, None),
    ("publish_to_gpu_avg_ms=2.300, callback_to_gpu_avg_ms=0.210", "publish_to_gpu", 2.3, 0.21),
])
def test_cuda_window_progress_reads_current_and_legacy_formats(latency_text, basis, latency, callback):
    progress = GpuBenchmarkProgress("CUDA_BUFFER", 60, "global")
    assert progress.consume_line(
        "[container] [INFO] [cuda/depth] Sampling 3.0/60 s, samples=90, recent_samples=30, "
        f"FPS=30.00, {latency_text}, cpu_percent=45.125, gpu_percent=46.581"
    )
    snapshot = progress.snapshot()
    row = snapshot["rows"][0]
    assert snapshot["latency_basis"] == basis
    assert snapshot["elapsed_seconds"] == 3.0
    assert row["samples"] == 90 and row["fps"] == 30.0 and row["backend"] == "cuda"
    assert row["latency_ms"] == latency and row.get("callback_to_gpu_ms") == callback
    assert snapshot["system"]["cuda"] == {"cpu_percent": 45.125, "gpu_percent": 46.581}


def test_cuda_summary_reports_end_to_end_latency_and_longest_gap(tmp_path):
    csv = tmp_path / "gpu_driver_summary.csv"
    csv.write_text(
        "stream,phase,status,received_frames,valid_frames,cpu_fallback_frames,read_errors,"
        "metric,values,mean,min,p95,max,stddev\n"
        "depth,cuda,complete,31,31,0,0,receive_fps,31,0.517,n/a,n/a,n/a,n/a\n"
        "depth,cuda,complete,31,31,0,0,longest_gap_sec,1,58.999,n/a,n/a,n/a,n/a\n"
        "depth,cuda,complete,31,31,0,0,end_to_end_latency_ms,31,35.499,20.0,40.153,50.0,1.0\n",
        encoding="utf-8",
    )
    rows, _ = read_gpu_summary(csv, "CUDA_BUFFER", "global")
    row = rows["cuda", "depth"]
    assert row["complete"] and row["fps"] == 0.517 and row["samples"] == 31
    assert row["latency_ms"] == 35.499 and row["latency_p95_ms"] == 40.153
    assert row["longest_gap_sec"] == 58.999
    markdown = build_performance_summary({
        "profile_name": "generic_performance",
        "transport_receiver": "gpu",
        "gpu_backend": "CUDA_BUFFER",
        "gpu_transport_summary": [row],
        "gpu_system_summary": {"cuda": {"cpu_percent": 45.125, "gpu_percent": 46.581}},
        "stream_config": [{"stream": "depth", "width": 640, "height": 480, "format": "Y16"}],
        "duration_seconds": 60,
    })
    assert "- Latency basis: Frame header timestamp to subscription callback" in markdown
    assert "- Test duration: 60 s" in markdown
    assert (
        "| Stream | Resolution | Format | Mode | Receiver backend | FREQ[Hz] | "
        "LATENCY AVG (ms) | LATENCY P95 (ms) | CPU[%] | GPU[%] |"
    ) in markdown
    assert "| depth | 640 x 480 | Y16 | CUDA Buffer | cuda | 0.517 | 35.499 | 40.153 | 45.125 | 46.581 |" in markdown


def test_nitros_device_clock_suppresses_legacy_latency_and_keeps_fps(tmp_path):
    progress = GpuBenchmarkProgress("NITROS_GXF", 60, "device")
    assert progress.consume_line(
        "[CPU (DDS)/depth] Sampling 1.0/60 s, samples=30, FPS=30.00, LATENCY_avg=1795587333952.0 ms"
    )
    assert progress.snapshot()["rows"][0]["latency_ms"] is None
    csv = tmp_path / "gpu_driver_summary.csv"
    csv.write_text(
        "phase,stream,duration_sec,samples,fps,latency_avg_ms,latency_p95_ms,"
        "read_errors,system_cpu_pct,system_gpu_pct,longest_gap_sec\n"
        "CPU (DDS),depth,60,31,0.517,1795587333952.0,1795587333952.0,0,50,14,58.999\n",
        encoding="utf-8",
    )
    rows, _ = read_gpu_summary(csv, "NITROS_GXF", "device")
    row = rows["CPU (DDS)", "depth"]
    assert row["latency_ms"] is None and row["latency_p95_ms"] is None
    assert row["fps"] == 0.517 and row["samples"] == 31 and row["longest_gap_sec"] == 58.999
