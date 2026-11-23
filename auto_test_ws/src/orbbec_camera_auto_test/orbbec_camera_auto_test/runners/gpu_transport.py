"""Run the GPU benchmark supplied by the selected Orbbec driver."""

import math
import time
from pathlib import Path

import yaml

from ..core.environment_info import camera_environment_from_launch_log
from ..core.gpu_benchmark import (
    GPU_PROGRESS_FILE, GPU_STREAMS, GpuBenchmarkProgress,
    camera_launch_options, gpu_benchmark_capability, read_gpu_summary,
)
from ..core.reporter import build_performance_summary, write_json, write_markdown
from ..core.session import TestSession, capture_runtime_env
from .functional import _parse_launch_args
from .transport import TRANSPORT_SCENARIO, _loaded_sdk_paths, _package_share, _sdk_metadata


def _enabled(value):
    return str(value).strip().lower() == "true"


def _prepare_camera(args, launch_args, defaults, camera_share, results_dir):
    settings = dict(launch_args)
    config_path = settings.get("config_file_path")
    overrides = {}
    if config_path:
        source = Path(config_path).expanduser()
        if not source.is_absolute() and not source.is_file():
            source = camera_share / "config" / source
        overrides = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            raise ValueError(f"Expected a camera parameter mapping in {source}")
        for key in ("serial_number", "usb_port"):
            if settings.get(key) and key in overrides and str(settings[key]) != str(overrides[key]):
                raise ValueError(f"Camera config overrides the requested {key}")
        overrides.update(_parse_launch_args(args.transport_preset_profile))
    effective = {**defaults, **settings, **overrides}
    if effective.get("device_preset") == "Dual Color Streams":
        raise ValueError("The installed GPU benchmark does not support the dual-color preset")
    for flag in ("enable_ir", "enable_left_color", "enable_right_color"):
        if _enabled(overrides.get(flag, settings.get(flag))):
            raise ValueError(f"The installed GPU benchmark does not support {flag.removeprefix('enable_')}")
        settings[flag] = False
    flags = {flag: _enabled(effective.get(flag)) for flag in GPU_STREAMS.values()}
    if flags["enable_point_cloud"] and not flags["enable_depth"]:
        raise ValueError("Enable depth to measure the depth point cloud")
    if flags["enable_colored_point_cloud"] and not (flags["enable_color"] and flags["enable_depth"]):
        raise ValueError("Enable color and depth to measure the colored point cloud")
    streams = {stream for stream, flag in GPU_STREAMS.items() if flags[flag]}
    if not streams:
        raise ValueError("Enable a GPU image or point cloud stream")
    required = {**flags, "enable_gpu_transport": True, "use_intra_process_comms": False}
    settings.update(required)
    settings.setdefault("time_domain", "global")
    if config_path:
        # The driver's parameter YAML has priority over its launch arguments.
        config = results_dir / "transport_camera_config.yaml"
        overrides.update(required)
        config.write_text(yaml.safe_dump(overrides, sort_keys=False), encoding="utf-8")
        settings["config_file_path"] = str(config)
    settings["camera_name"] = str(effective.get("camera_name") or "camera")
    return settings, streams


def _write_progress(path, progress, *, final=False):
    temporary = path.with_suffix(".tmp")
    write_json(temporary, progress.snapshot(final=final))
    temporary.replace(path)


def run_gpu_transport_test(
    args, launch_args, results_dir, host_environment, duration, emit_status, stream_config_reader
):
    result = {
        "profile_name": "generic_performance", "scenario_name": TRANSPORT_SCENARIO,
        "transport_receiver": "gpu", "ros_version": str(args.ros_version),
        "launch_file": args.launch_file, "camera_name": launch_args.get("camera_name", "camera"),
        "duration_seconds": duration, "warmup_frames": args.transport_warmup_frames,
        "warmup_timeout_sec": 60, "launch_args": launch_args,
        "environment": {"host": host_environment, "driver_setup": args.driver_setup},
        "status": "failed",
    }
    session = None
    sdk_paths = set()
    launch_log = results_dir / "launch.log"
    progress_path = results_dir / GPU_PROGRESS_FILE
    try:
        if args.ros_version != "2":
            raise ValueError("GPU transport requires ROS 2")
        if not math.isfinite(duration) or not duration.is_integer() or not 1 <= duration <= 3600:
            raise ValueError("GPU sampling duration must be an integer from 1 to 3600 seconds per phase")
        if not 0 <= args.transport_warmup_frames <= 100000:
            raise ValueError("GPU warmup frames must be within 0..100000")
        env = capture_runtime_env(args.driver_setup, ros_setup=args.ros_setup)
        camera_share = _package_share("orbbec_camera", env)
        result["environment"].update(
            driver_share=str(camera_share), rmw_implementation=env.get("RMW_IMPLEMENTATION", ""),
        )
        camera_launch = Path(args.launch_file).name
        installed_launch = camera_share / "launch" / camera_launch
        if (Path(args.launch_file).parent != Path(".")
                and Path(args.launch_file).resolve() != installed_launch.resolve()):
            raise ValueError("GPU transport requires a camera launch installed in the selected driver")
        defaults = camera_launch_options(camera_launch, env)
        backend, launcher, issue = gpu_benchmark_capability(camera_share, defaults)
        if issue:
            raise RuntimeError(issue)
        progress = GpuBenchmarkProgress(backend, int(duration))
        settings, streams = _prepare_camera(args, launch_args, defaults, camera_share, results_dir)
        result["camera_name"] = settings["camera_name"]
        result["gpu_backend"] = backend
        result["transport_mode"] = "cross_process" if backend == "CUDA_BUFFER" else "intra_process"
        config = results_dir / "gpu_subscribers_params.yaml"
        raw_summary = results_dir / "gpu_driver_summary.csv"
        config.write_text(yaml.safe_dump({
            "camera_launch": camera_launch, "camera": settings,
            "benchmark": {"duration_sec": int(duration), "warmup_samples": args.transport_warmup_frames,
                          "csv_log_file": str(raw_summary)},
        }, sort_keys=False), encoding="utf-8")
        _write_progress(progress_path, progress)
        session = TestSession(
            str(launcher), {"config_file_path": config},
            results_dir, launch_log, runtime_env=env, status_callback=emit_status,
        )
        emit_status(f"Starting {backend} benchmark, {duration:g} s per phase")
        session.start()
        deadline = time.monotonic() + 2 * (60 + duration) + 15
        while session.is_running():
            if time.monotonic() >= deadline:
                raise RuntimeError("GPU benchmark timed out")
            if progress.consume(launch_log):
                _write_progress(progress_path, progress)
            if not sdk_paths:
                sdk_paths = _loaded_sdk_paths(session.root_pid)
            time.sleep(0.5)
        exit_code = session.process.returncode
        session.stop()
        progress.consume(launch_log)
        rows, system = read_gpu_summary(raw_summary, backend)
        if rows:
            progress.rows = rows
        _write_progress(progress_path, progress, final=bool(rows))
        result["gpu_transport_summary"] = list(rows.values())
        result["gpu_system_summary"] = system
        phases = ("cpu", "cuda") if backend == "CUDA_BUFFER" else ("CPU (DDS)", "NITROS (IPC)")
        if exit_code or any(not rows.get((phase, stream), {}).get("complete")
                            for phase in phases for stream in streams):
            with launch_log.open("rb") as output:
                output.seek(max(0, launch_log.stat().st_size - 16384))
                tail = output.read().decode("utf-8", errors="replace").splitlines()
            failures = [line for line in tail if "[ERROR]" in line or "GPU transport fallback" in line]
            details = "\n".join(failures[-8:] or tail[-8:])
            raise RuntimeError(f"{backend} benchmark did not complete all selected streams\n{details}")
        result["status"] = "passed"
    except KeyboardInterrupt:
        result.update(status="interrupted", error="Interrupted by user")
    except Exception as error:
        result["error"] = str(error)
        emit_status(f"GPU transport test failed: {error}")
    finally:
        if session is not None and session.process is not None:
            session.stop()
        result["environment"]["loaded_sdk"] = _sdk_metadata(sdk_paths)
        result["environment"]["camera"] = camera_environment_from_launch_log(
            launch_log, result["camera_name"]
        )
        result["stream_config"] = stream_config_reader(launch_log)
        write_json(results_dir / "result.json", result)
        write_markdown(results_dir / "summary.md", build_performance_summary(result))
    return {"passed": 0, "interrupted": 130, "failed": 1}[result["status"]]
