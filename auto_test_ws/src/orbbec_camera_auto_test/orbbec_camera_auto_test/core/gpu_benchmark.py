"""Capabilities and output formats of the installed Orbbec GPU examples."""

import csv
import math
import re
import subprocess


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


def camera_launch_options(launch_file, env):
    process = subprocess.run(
        ["ros2", "launch", "orbbec_camera", launch_file, "--show-args"],
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


def gpu_benchmark_capability(camera_share, arguments):
    resources = camera_share.parent / "ament_index" / "resource_index"
    marker = resources / "orbbec_gpu_backend" / "orbbec_camera"
    backend = marker.read_text(encoding="utf-8").strip() if marker.is_file() else "NONE"
    launcher = GPU_LAUNCHES.get(backend)
    if not launcher:
        return backend, f"The selected driver has no GPU benchmark backend ({backend})"
    if not (camera_share / "launch" / launcher).is_file():
        return backend, f"The selected driver does not install {launcher}"
    if "enable_gpu_transport" not in arguments:
        return backend, "The selected camera launch does not expose enable_gpu_transport"
    if backend != "CUDA_BUFFER" and "component_container_name" not in arguments:
        return backend, "NITROS requires a camera launch with component_container_name"
    return backend, ""


def number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


NITROS_SAMPLE = re.compile(
    r"\[(CPU \(DDS\)|NITROS \(IPC\))/(\w+)\] Sampling ([\d.]+)/\d+ s,"
    r" samples=(\d+), FPS=([\d.]+), LATENCY_avg=(\S+) ms"
)
CUDA_SAMPLE = re.compile(
    r"\[(cpu|cuda)/(\w+)\] Sample (\d+) elapsed=([\d.]+)/\d+ s backend=(\w+)"
    r" fps=(\S+) publish_to_gpu_ms=(\S+) callback_to_gpu_ms=([\d.]+)"
)
CUDA_FALLBACK = re.compile(r"\[(\w+)\] Requested cuda, received \w+\..*CPU fallback: (\d+)")
PHASE_START = re.compile(r"Starting (CPU \(DDS\)|NITROS \(IPC\)|cpu|cuda) phase\b")


class GpuBenchmarkProgress:
    def __init__(self, backend, duration):
        self.backend = backend
        self.duration = duration
        self.phase = ""
        self.rows = {}
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
            match = CUDA_SAMPLE.search(line)
            if not match:
                return False
            phase, stream, samples, elapsed, backend, fps, latency, callback = match.groups()
        else:
            match = NITROS_SAMPLE.search(line)
            if not match:
                return False
            phase, stream, elapsed, samples, fps, latency = match.groups()
            backend = "cpu" if phase == "CPU (DDS)" else self.backend
            callback = None
        if phase != self.phase:
            self.phase = phase
            self.elapsed = 0.0
        self.elapsed = max(self.elapsed, float(elapsed))
        row = self.rows.setdefault((phase, stream), {"phase": phase, "stream": stream})
        row.update(
            backend=backend if int(samples) else None,
            samples=int(samples), fps=number(fps), latency_ms=number(latency),
            callback_to_gpu_ms=number(callback),
        )
        return True

    def snapshot(self, *, final=False):
        return {
            "backend": self.backend,
            "phase": self.phase,
            "elapsed_seconds": self.duration if final else self.elapsed,
            "duration_seconds": self.duration,
            "final": final,
            "rows": list(self.rows.values()),
        }


def read_gpu_summary(path, backend):
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
                elif metric == "publish_to_gpu_ms":
                    row.update(latency_ms=number(entry["mean"]), latency_p95_ms=number(entry["p95"]))
                elif metric == "callback_to_gpu_ms":
                    row.update(callback_to_gpu_ms=number(entry["mean"]),
                               callback_to_gpu_p95_ms=number(entry["p95"]))
                row["backend"] = phase if row["samples"] else "cpu" if row["cpu_fallback_frames"] else None
            else:
                samples = int(entry["samples"])
                rows[phase, stream] = {
                    "phase": phase, "stream": stream, "samples": samples,
                    "backend": ("cpu" if phase == "CPU (DDS)" else backend) if samples else None,
                    "fps": number(entry["fps"]), "latency_ms": number(entry["latency_avg_ms"]),
                    "latency_p95_ms": number(entry["latency_p95_ms"]),
                    "read_errors": int(entry["read_errors"]), "complete": samples > 0,
                }
                system[phase] = {
                    "cpu_percent": number(entry.get("system_cpu_pct")),
                    "gpu_percent": number(entry.get("system_gpu_pct")),
                }
    return rows, system
