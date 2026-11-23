from orbbec_camera_auto_test.core.gpu_benchmark import GpuBenchmarkProgress, read_gpu_summary


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
    assert rows["cuda", "depth"]["latency_p95_ms"] == 3.0
    assert rows["cuda", "depth"]["callback_to_gpu_p95_ms"] == 0.3
    assert rows["cuda", "depth"]["cpu_fallback_frames"] == 3
    assert system["cuda"]["gpu_percent"] == 12.0
