"""Measure standard ROS 2 transport with an independent C++ subscriber."""

from __future__ import annotations

import csv
import hashlib
import math
import re
import subprocess
import time
from pathlib import Path

import psutil
import yaml

from ..core.environment_info import camera_environment_from_launch_log
from ..core.gpu_benchmark import FRAME_TO_CALLBACK, GPU_PROGRESS_FILE, driver_gpu_backend, group_gpu_rows, number
from ..core.reporter import build_performance_summary, write_json, write_markdown
from ..core.session import TestSession, capture_runtime_env, default_ros_setup
from ..core.transport_build import TRANSPORT_PACKAGE, ensure_transport_setup
from ..core.transport_config import GPU_BACKENDS, prepare_transport_config
from .performance_system import ProcessTreeSampler
from .functional import _parse_launch_args

TRANSPORT_SCENARIO = "transport_latency"
TRANSPORT_MODES = ("cross_process", "intra_process")
TRANSPORT_RELIABILITIES = ("best_effort", "reliable")
DEFAULT_WARMUP_FRAMES = 30
DEFAULT_WARMUP_TIMEOUT_SEC = 60.0


def _package_share(name, env):
    completed = subprocess.run(
        ["ros2", "pkg", "prefix", "--share", name], env=env, capture_output=True,
        text=True, check=True, timeout=15,
    )
    return Path(completed.stdout.strip())


def _configured_setup(args, backend="NONE"):
    if args.transport_setup:
        return str(Path(args.transport_setup).expanduser().resolve())
    workspace = Path(__file__).resolve().parents[4]
    if (workspace / "src" / TRANSPORT_PACKAGE).is_dir():
        return ensure_transport_setup(
            workspace, args.ros_setup or default_ros_setup(), backend=backend,
            driver_setup=args.driver_setup if backend != "NONE" else None,
        )
    return None


def _loaded_sdk_paths(pid):
    paths = set()
    try:
        processes = [psutil.Process(pid)]
        processes.extend(processes[0].children(recursive=True))
    except psutil.Error:
        return paths
    for process in processes:
        try:
            with Path(f"/proc/{process.pid}/maps").open(encoding="utf-8") as mappings:
                for mapping in mappings:
                    fields = mapping.rstrip().split(maxsplit=5)
                    if len(fields) == 6 and "/libOrbbecSDK.so" in fields[5]:
                        paths.add(fields[5])
        except OSError:
            continue
    return paths


def _sdk_metadata(paths):
    metadata = []
    for path in sorted(paths):
        entry = {"path": path}
        try:
            digest = hashlib.sha256()
            with Path(path).open("rb") as library:
                for chunk in iter(lambda: library.read(1024 * 1024), b""):
                    digest.update(chunk)
            entry["sha256"] = digest.hexdigest()
        except OSError:
            entry["sha256"] = None
        metadata.append(entry)
    return metadata


def _read_summary(path, reliability):
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
        if any(row.get("transport_reliability", "best_effort") != reliability for row in rows):
            raise RuntimeError(
                f"The transport test component did not report the requested QoS {reliability}; "
                "rebuild the test component for the selected ROS 2 environment"
            )
        return {
            row["topic"]: {
                "message_count": int(row["samples"]),
                "avg_fps": float(row["avg_fps"]),
                "latency_avg_ms": float(row["latency_avg_ms"]) if row["latency_avg_ms"] else None,
                "latency_p95_ms": float(row["latency_p95_ms"]) if row["latency_p95_ms"] else None,
                "invalid_stamps": int(row["invalid_stamps"]),
                "delivery_mismatches": int(row["delivery_mismatches"]),
                **({"camera_name": row["camera_name"], "stream": row["stream"],
                    "longest_gap_sec": number(row.get("longest_gap_sec"))}
                   if row.get("camera_name") else {}),
            }
            for row in rows
        }


def _read_configured_gpu_summary(path, backend, duration, *, final):
    rows = []
    system = {}
    phase = ""
    elapsed = 0.0
    if path and path.is_file():
        with path.open(newline="", encoding="utf-8") as file:
            for values in csv.DictReader(file):
                phase = values["phase"]
                elapsed = float(values["elapsed_seconds"])
                done = elapsed >= duration
                rows.append({
                    "camera_name": values["camera_name"], "stream": values["stream"],
                    "topic": values["topic"], "phase": phase, "backend": values["backend"],
                    "samples": int(values["samples"]), "complete": values["complete"] == "true",
                    "fps": number(values["avg_fps"] if done else values["current_fps"]),
                    "latency_basis": FRAME_TO_CALLBACK,
                    "latency_ms": number(values["latency_avg_ms"]),
                    "latency_p95_ms": number(values["latency_p95_ms"]),
                    "longest_gap_sec": number(values["longest_gap_sec"]),
                    "invalid_stamps": int(values["invalid_stamps"]),
                    "delivery_mismatches": int(values["delivery_mismatches"]),
                    "read_errors": int(values["read_errors"]),
                    "cpu_fallback_frames": int(values["cpu_fallback_frames"]),
                })
                system[phase] = {
                    "cpu_percent": number(values["system_cpu_pct"]),
                    "gpu_percent": number(values["system_gpu_pct"]),
                }
    return {
        "backend": backend, "phase": phase, "elapsed_seconds": elapsed, "duration_seconds": duration,
        "latency_basis": FRAME_TO_CALLBACK, "rows": group_gpu_rows(rows), "system": system,
        "final": final, "configured": True,
    }


def _write_gpu_progress(directory, summary):
    path = directory / GPU_PROGRESS_FILE
    temporary = path.with_suffix(".tmp")
    write_json(temporary, summary)
    temporary.replace(path)


def _missing_warmup_topics(path, required_frames):
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as file:
        return [
            f"{row['phase']} {row['topic']}" for row in csv.DictReader(file)
            if float(row["elapsed_seconds"]) == 0.0 and row.get("warmup_received")
            and int(row["warmup_received"]) < max(1, required_frames)
        ]


def _camera_config_with_profiles(camera_launch, config_file, profiles, results_dir):
    config_path = Path(config_file).expanduser()
    if not config_path.is_absolute() and not config_path.is_file():
        config_path = camera_launch.parent.parent / "config" / config_path
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"Expected a camera parameter mapping in {config_path}")
    for name in profiles:
        if name not in config or not re.fullmatch(
            r"(left_color|right_color|left_ir|right_ir)_(width|height|fps|format)", name
        ):
            raise ValueError(f"Unsupported stream profile option {name} for {config_path.name}")
    config.update(profiles)
    target = results_dir / "transport_camera_config.yaml"
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(target)


def run_transport_test(
    args, launch_args, results_dir, host_environment, duration, emit_status, stream_config_reader
):
    camera_plan = getattr(args, "transport_camera_config", None)
    gpu = bool(camera_plan and args.transport_receiver == "gpu")
    mode = "cross_process" if gpu else args.transport_mode
    reliability = "best_effort" if gpu else args.transport_reliability
    warmup_frames = args.transport_warmup_frames
    warmup_timeout = args.transport_warmup_timeout_sec
    result = {
        "profile_name": "generic_performance",
        "scenario_name": TRANSPORT_SCENARIO,
        "transport_mode": mode,
        "transport_reliability": reliability,
        "ros_version": str(args.ros_version),
        "launch_file": args.launch_file,
        "camera_name": launch_args.get("camera_name", "camera"),
        "duration_seconds": duration,
        "warmup_frames": warmup_frames,
        "warmup_timeout_sec": warmup_timeout,
        "launch_args": launch_args,
        "environment": {"host": host_environment, "driver_setup": args.driver_setup},
        "transport_summary": {},
        "system_summary": {},
        "status": "failed",
    }
    session = None
    sampler = None
    backend = "NONE"
    sdk_paths = set()
    launch_log = results_dir / "launch.log"
    try:
        if args.ros_version != "2":
            raise ValueError("Transport latency requires ROS 2")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Transport sampling duration must be positive")
        if gpu:
            result["transport_receiver"] = "gpu"
            if not float(duration).is_integer() or not 1 <= duration <= 3600 or not 0 <= warmup_frames <= 100000:
                raise ValueError("GPU sampling duration must be 1–3600 integer seconds and warmup frames 0–100000")
        elif not 1 <= warmup_frames < 2**63:
            raise ValueError("Transport warmup frames must be a positive integer within the ROS 2 integer range")
        if not math.isfinite(warmup_timeout) or warmup_timeout <= 0:
            raise ValueError("Transport warmup timeout must be positive")

        selected_env = capture_runtime_env(args.driver_setup, ros_setup=args.ros_setup)
        camera_share = _package_share("orbbec_camera", selected_env)
        result["environment"]["driver_share"] = str(camera_share)
        if gpu:
            backend = driver_gpu_backend(camera_share)
            if backend not in GPU_BACKENDS:
                raise RuntimeError(f"The selected driver has no supported GPU backend ({backend})")
            result["gpu_backend"] = backend
            if backend.startswith("NITROS"):
                result["transport_mode"] = "intra_process"
        if camera_plan:
            launches, streams = prepare_transport_config(
                camera_plan, camera_share, selected_env, backend, mode == "intra_process",
            )
            result["camera_names"] = list(dict.fromkeys(stream["camera_name"] for stream in streams))
            result["transport_camera_config"] = str(camera_plan)
            result["launch_file"] = ", ".join(launch["path"] for launch in launches)
            result["launch_args"] = {}
        tool_setup = _configured_setup(args, backend)
        env = capture_runtime_env(
            args.driver_setup, ros_setup=args.ros_setup, tool_setup=tool_setup
        )
        if _package_share("orbbec_camera", env).resolve() != camera_share.resolve():
            raise RuntimeError("The test setup overrides the selected camera driver")
        try:
            test_share = _package_share(TRANSPORT_PACKAGE, env)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"The selected test environment did not load {TRANSPORT_PACKAGE}"
            ) from exc
        built_distro = (test_share / "ros_distro.txt").read_text(encoding="utf-8").strip()
        running_distro = env.get("ROS_DISTRO", "")
        if built_distro and built_distro != running_distro:
            raise RuntimeError(f"The test component was built for ROS {built_distro}, selected {running_distro}")
        if gpu:
            built_backend = test_share / "gpu_backend.txt"
            if not built_backend.is_file() or built_backend.read_text(encoding="utf-8").strip() != backend:
                raise RuntimeError(f"The transport test component must be built for {backend}; "
                                   "select a matching test setup or clear the setup override")
        result["environment"]["probe_share"] = str(test_share)
        result["environment"]["rmw_implementation"] = env.get("RMW_IMPLEMENTATION", "")

        if camera_plan:
            inputs = {"launches": launches, "streams": streams, "gpu_backend": backend}
        else:
            camera_launch = Path(args.launch_file).expanduser()
            if not camera_launch.is_absolute() and camera_launch.parent == Path("."):
                camera_launch = camera_share / "launch" / camera_launch
            camera_launch = camera_launch.resolve(strict=True)
            camera_args = dict(launch_args)
            driver_config = camera_args.get("config_file_path")
            preset_profiles = _parse_launch_args(args.transport_preset_profile)
            if preset_profiles:
                if not driver_config:
                    raise ValueError("Preset stream profiles require a camera configuration file")
                for name in preset_profiles:
                    preset_profiles[name] = camera_args.pop(name, preset_profiles[name])
                camera_args["config_file_path"] = _camera_config_with_profiles(
                    camera_launch, driver_config, preset_profiles, results_dir
                )
                result["preset_stream_profiles"] = preset_profiles
            elif driver_config:
                local_config = Path(driver_config).expanduser()
                if local_config.is_file():
                    camera_args["config_file_path"] = str(local_config.resolve())
            inputs = {"camera_launch": str(camera_launch), "camera_arguments": camera_args}
        config_file = results_dir / "transport_config.json"
        write_json(config_file, {
            "transport_mode": mode,
            "transport_reliability": reliability,
            "duration_seconds": duration,
            "warmup_frames": warmup_frames,
            "warmup_timeout_sec": warmup_timeout,
            **inputs,
        })
        progress_path = results_dir / "transport_progress.csv"
        phase_path = results_dir / "transport_phase_summary.csv"
        summary_path = results_dir / "transport_summary.csv"
        if gpu:
            _write_gpu_progress(results_dir, _read_configured_gpu_summary(None, backend, duration, final=False))
        session = TestSession(
            str(test_share / "launch" / "transport_benchmark.launch.py"),
            {"benchmark_config": config_file}, results_dir, launch_log, runtime_env=env,
            status_callback=emit_status,
        )
        if gpu:
            emit_status(f"Starting {backend} transport comparison with {camera_share} for {duration:g} s per phase")
        else:
            emit_status(f"Starting {mode} transport test with {camera_share} for {duration:g} s")
        session.start()
        # Allow additional time for ROS launch startup and shutdown.
        deadline = time.monotonic() + (2 if gpu else 1) * (warmup_timeout + duration) + 15.0
        sampling_start = None
        next_sample = 0.0
        while session.process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError("Transport benchmark timed out")
            if now >= next_sample:
                next_sample = now + 1.0
                if not sdk_paths:
                    sdk_paths = _loaded_sdk_paths(session.root_pid)
                # The subscriber writes its summary before ROS launch tears down the camera.
                if gpu:
                    candidates = [path for path in (progress_path, phase_path) if path.is_file()]
                    if candidates:
                        latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
                        _write_gpu_progress(results_dir, _read_configured_gpu_summary(
                            latest, backend, duration, final=False,
                        ))
                elif progress_path.is_file() and not summary_path.is_file():
                    if sampler is None:
                        sampling_start = now
                        sampler = ProcessTreeSampler(session, results_dir / "system_usage.csv")
                        emit_status(f"Collecting {mode} transport samples")
                    sampler.sample(now - sampling_start)
            time.sleep(0.1)

        if gpu:
            gpu_result = _read_configured_gpu_summary(
                summary_path if summary_path.is_file() else phase_path, backend, duration, final=True,
            )
            _write_gpu_progress(results_dir, gpu_result)
            result["gpu_transport_summary"] = gpu_result["rows"]
            result["gpu_system_summary"] = gpu_result["system"]
        elif camera_plan and phase_path.is_file() and not summary_path.is_file():
            result["transport_summary"] = _read_summary(phase_path, reliability)
        if not summary_path.is_file():
            details = launch_log.read_text(encoding="utf-8", errors="replace").splitlines()
            errors = [line for line in details if "[ERROR]" in line]
            missing = _missing_warmup_topics(phase_path, warmup_frames) if camera_plan else []
            message = "Transport sampling did not finish"
            if missing:
                message += ". Missing warmup frames on " + ", ".join(missing)
            raise RuntimeError(message + "\n" + "\n".join((errors or details)[-8:]))
        if session.process.returncode:
            raise RuntimeError(f"Transport launch exited with code {session.process.returncode}")
        if gpu:
            phases = ("CPU (DDS)", "CUDA Buffer" if backend == "CUDA_BUFFER" else "NITROS (IPC)")
            complete = {(row["phase"], row["topic"]) for row in gpu_result["rows"] if row["complete"]}
            missing = [f"{phase} {stream['topic']}" for phase in phases for stream in streams
                       if (phase, stream["topic"]) not in complete]
            if missing:
                raise RuntimeError("Incomplete GPU sampling for: " + ", ".join(missing))
        else:
            result["transport_summary"] = _read_summary(summary_path, reliability)
            if not result["transport_summary"]:
                raise RuntimeError("No selected streams produced a summary")
            missing = [
                topic for topic, stats in result["transport_summary"].items()
                if not stats["message_count"] or stats["latency_avg_ms"] is None
            ]
            if missing:
                raise RuntimeError(
                    f"No frames with comparable wall-clock timestamps for: {', '.join(missing)}"
                )
        result["status"] = "passed"
    except KeyboardInterrupt:
        result.update(status="interrupted", error="Interrupted by user")
    except Exception as exc:
        result["error"] = str(exc)
        emit_status(f"Transport test failed: {exc}")
    finally:
        if sampler is not None:
            result["system_summary"] = sampler.build_summary()
            sampler.close()
        if session is not None:
            session.stop()
        result["environment"]["loaded_sdk"] = _sdk_metadata(sdk_paths)
        if result.get("camera_names"):
            result["environment"]["cameras"] = {
                name: camera_environment_from_launch_log(launch_log, name)
                for name in result["camera_names"]
            }
        else:
            result["environment"]["camera"] = camera_environment_from_launch_log(
                launch_log, result["camera_name"]
            )
        result["stream_config"] = stream_config_reader(launch_log)
        write_json(results_dir / "result.json", result)
        write_markdown(results_dir / "summary.md", build_performance_summary(result))
    return {"passed": 0, "interrupted": 130, "failed": 1}[result["status"]]
