"""Capabilities and output formats of the installed Orbbec GPU examples."""

import csv
import math
import re
import subprocess
from pathlib import Path


GPU_LAUNCHES = {
    "NITROS_GXF": "camera_nitros_subscriber.launch.py",
    "NITROS_BUFFER": "camera_nitros_subscriber.launch.py",
    "CUDA_BUFFER": "camera_cuda_buffer_subscriber.launch.py",
}
GPU_STREAMS = {
    "color": "enable_color",
    "depth": "enable_depth",
    "left_ir": "enable_left_ir",
    "right_ir": "enable_right_ir",
    "points": "enable_point_cloud",
    "colored_points": "enable_colored_point_cloud",
}
GPU_PROGRESS_FILE = "gpu_transport_progress.json"
FRAME_TO_CALLBACK = "frame_to_callback"
PUBLISH_TO_GPU = "publish_to_gpu"
LATENCY_METRICS = {
    "end_to_end_latency_ms": FRAME_TO_CALLBACK,
    "publish_to_gpu_ms": PUBLISH_TO_GPU,
}


def camera_launch_options(launch_file, env):
    command = ["ros2", "launch"]
    if isinstance(launch_file, Path):
        command.append(str(launch_file))
    else:
        command.extend(("orbbec_camera", launch_file))
    process = subprocess.run(
        [*command, "--show-args"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    if process.returncode:
        raise ValueError(f"Could not read arguments for {launch_file}")
    blocks = re.split(r"(?m)^ +['\"]([\w]+)['\"]:\s*$", process.stdout)
    defaults = {}
    for name, description in zip(blocks[1::2], blocks[2::2]):
        value = re.search(r"\(default: ['\"](.*?)['\"]\)", description)
        defaults[name] = value[1] if value else None
    return defaults


def driver_gpu_backend(camera_share):
    resources = camera_share.parent / "ament_index" / "resource_index"
    marker = resources / "orbbec_gpu_backend" / "orbbec_camera"
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else "NONE"


def gpu_benchmark_capability(camera_share, arguments):
    """Return the backend, usable benchmark launch path, and any validation error."""
    backend = driver_gpu_backend(camera_share)
    launch_name = GPU_LAUNCHES.get(backend)
    if not launch_name:
        return backend, None, f"The selected driver has no GPU benchmark backend ({backend})"
    candidates = (
        camera_share / "examples" / "gpu_subscribers" / launch_name,
        camera_share / "launch" / launch_name,
    )
    launcher = next((path for path in candidates if path.is_file()), None)
    if launcher is None:
        return backend, None, f"The selected driver does not install {launch_name}"
    if "enable_gpu_transport" not in arguments:
        return backend, None, "The selected camera launch does not expose enable_gpu_transport"
    if backend != "CUDA_BUFFER" and "component_container_name" not in arguments:
        return backend, None, "NITROS requires a camera launch with component_container_name"
    return backend, launcher, ""


def number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def latency_value(value, basis, time_domain):
    if basis == FRAME_TO_CALLBACK and time_domain not in (None, "global", "system"):
        return None
    return number(value)


def gpu_latency_basis(backend, rows):
    basis = next((row["latency_basis"] for row in rows if row.get("latency_basis")), None)
    if basis:
        return basis
    if backend in ("NITROS_GXF", "NITROS_BUFFER"):
        return FRAME_TO_CALLBACK
    return PUBLISH_TO_GPU if rows and backend == "CUDA_BUFFER" else None


def group_gpu_rows(rows):
    streams = {}
    for row in rows:
        streams.setdefault((row.get("camera_name"), row["stream"]), []).append(row)
    return [row for stream_rows in streams.values() for row in stream_rows]


NITROS_SAMPLE = re.compile(
    r"\[(CPU \(DDS\)|NITROS \(IPC\))/(\w+)\] Sampling ([\d.]+)/\d+ s,"
    r" samples=(\d+), FPS=([\d.]+), LATENCY_avg=(\S+) ms"
)
CUDA_SAMPLE = re.compile(
    r"\[(cpu|cuda)/(\w+)\] Sample (\d+) elapsed=([\d.]+)/\d+ s backend=(\w+)"
    r" fps=(\S+) publish_to_gpu_ms=(\S+) callback_to_gpu_ms=([\d.]+)"
)
CUDA_WINDOW = re.compile(
    r"\[(cpu|cuda)/(\w+)\] Sampling ([\d.]+)/\d+ s,"
    r" samples=(\d+), recent_samples=\d+, FPS=([\d.]+), "
    r"(?:LATENCY_avg=(\S+) ms|publish_to_gpu_avg_ms=(\S+), callback_to_gpu_avg_ms=(\S+)), "
    r"cpu_percent=(\S+), gpu_percent=(\S+)"
)
CUDA_FALLBACK = re.compile(r"\[(\w+)\] Requested cuda, received \w+\..*CPU fallback: (\d+)")
PHASE_START = re.compile(r"Starting (CPU \(DDS\)|NITROS \(IPC\)|cpu|cuda) phase\b")


class GpuBenchmarkProgress:
    def __init__(self, backend, duration, time_domain=None):
        self.backend = backend
        self.duration = duration
        self.time_domain = time_domain
        self.phase = ""
        self.rows = {}
        self.system = {}
        self.elapsed = 0.0
        self.offset = 0

    def consume(self, path):
        if not path.is_file():
            return False
        changed = False
        with path.open(encoding="utf-8", errors="replace") as log:
            log.seek(self.offset)
            while line := log.readline():
                if not line.endswith("\n"):
                    break
                self.offset = log.tell()
                changed |= self.consume_line(line)
        return changed

    def consume_line(self, line):
        if match := PHASE_START.search(line):
            self.phase = match[1]
            self.elapsed = 0.0
            return True
        if match := CUDA_FALLBACK.search(line):
            stream, count = match.groups()
            row = self.rows.setdefault(("cuda", stream), {
                "phase": "cuda", "stream": stream, "backend": "cpu",
            })
            row["cpu_fallback_frames"] = int(count)
            return True
        if self.backend == "CUDA_BUFFER":
            if match := CUDA_WINDOW.search(line):
                (phase, stream, elapsed, samples, fps, frame_latency, publish_latency, callback,
                 cpu, gpu) = match.groups()
                backend = phase
                basis = FRAME_TO_CALLBACK if frame_latency is not None else PUBLISH_TO_GPU
                latency = frame_latency if frame_latency is not None else publish_latency
                self.system[phase] = {"cpu_percent": number(cpu), "gpu_percent": number(gpu)}
            elif match := CUDA_SAMPLE.search(line):
                phase, stream, samples, elapsed, backend, fps, latency, callback = match.groups()
                basis = PUBLISH_TO_GPU
            else:
                return False
        else:
            match = NITROS_SAMPLE.search(line)
            if not match:
                return False
            phase, stream, elapsed, samples, fps, latency = match.groups()
            backend = "cpu" if phase == "CPU (DDS)" else self.backend
            basis = FRAME_TO_CALLBACK
            callback = None
        if phase != self.phase:
            self.phase = phase
            self.elapsed = 0.0
        self.elapsed = max(self.elapsed, float(elapsed))
        row = self.rows.setdefault((phase, stream), {"phase": phase, "stream": stream})
        row.update(
            backend=backend if int(samples) else ("cpu" if row.get("cpu_fallback_frames") else None),
            samples=int(samples), fps=number(fps), latency_basis=basis,
            latency_ms=latency_value(latency, basis, self.time_domain),
        )
        if callback is not None:
            row["callback_to_gpu_ms"] = number(callback)
        return True

    def snapshot(self, *, final=False):
        rows = group_gpu_rows(self.rows.values())
        return {
            "backend": self.backend,
            "phase": self.phase,
            "elapsed_seconds": self.duration if final else self.elapsed,
            "duration_seconds": self.duration,
            "final": final,
            "latency_basis": gpu_latency_basis(self.backend, rows),
            "rows": rows,
            "system": self.system,
        }


def read_gpu_summary(path, backend, time_domain=None):
    rows = {}
    system = {}
    if not path.is_file():
        return rows, system
    with path.open(newline="", encoding="utf-8") as file:
        for entry in csv.DictReader(file):
            phase, stream = entry["phase"], entry["stream"]
            if backend == "CUDA_BUFFER":
                metric = entry["metric"]
                if stream == "system":
                    system.setdefault(phase, {})[metric] = number(entry["mean"])
                    continue
                row = rows.setdefault((phase, stream), {"phase": phase, "stream": stream})
                row.update(
                    samples=int(entry["valid_frames"]),
                    cpu_fallback_frames=int(entry["cpu_fallback_frames"]),
                    read_errors=int(entry["read_errors"]),
                    complete=entry["status"] == "complete",
                )
                if metric == "receive_fps":
                    row["fps"] = number(entry["mean"])
                elif metric in LATENCY_METRICS:
                    basis = LATENCY_METRICS[metric]
                    row.update(
                        latency_basis=basis,
                        latency_ms=latency_value(entry["mean"], basis, time_domain),
                        latency_p95_ms=latency_value(entry["p95"], basis, time_domain),
                    )
                elif metric == "longest_gap_sec":
                    row["longest_gap_sec"] = number(entry["mean"])
                elif metric == "callback_to_gpu_ms":
                    row.update(callback_to_gpu_ms=number(entry["mean"]),
                               callback_to_gpu_p95_ms=number(entry["p95"]))
                row["backend"] = phase if row["samples"] else "cpu" if row["cpu_fallback_frames"] else None
            else:
                samples = int(entry["samples"])
                rows[phase, stream] = {
                    "phase": phase, "stream": stream, "samples": samples,
                    "backend": ("cpu" if phase == "CPU (DDS)" else backend) if samples else None,
                    "fps": number(entry["fps"]), "latency_basis": FRAME_TO_CALLBACK,
                    "latency_ms": latency_value(entry["latency_avg_ms"], FRAME_TO_CALLBACK, time_domain),
                    "latency_p95_ms": latency_value(entry["latency_p95_ms"], FRAME_TO_CALLBACK, time_domain),
                    "longest_gap_sec": number(entry.get("longest_gap_sec")),
                    "read_errors": int(entry["read_errors"]), "complete": samples > 0,
                }
                system[phase] = {
                    "cpu_percent": number(entry.get("system_cpu_pct")),
                    "gpu_percent": number(entry.get("system_gpu_pct")),
                }
    return rows, system
