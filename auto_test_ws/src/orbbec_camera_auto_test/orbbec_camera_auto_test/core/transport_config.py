"""Resolve explicit camera and topic lists for the transport benchmark."""

import re
from pathlib import Path

import yaml

from .gpu_benchmark import camera_launch_options


STREAM_TOPICS = {
    "color": "color/image_raw",
    "depth": "depth/image_raw",
    "ir": "ir/image_raw",
    "left_ir": "left_ir/image_raw",
    "right_ir": "right_ir/image_raw",
    "left_color": "left_color/image_raw",
    "right_color": "right_color/image_raw",
    "points": "depth/points",
    "colored_points": "depth_registered/points",
}
POINT_STREAMS = {"points", "colored_points"}
ROS_NAME = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*(?:/[a-zA-Z_][a-zA-Z_0-9]*)*")
GPU_BACKENDS = {"NITROS_GXF", "NITROS_BUFFER", "CUDA_BUFFER"}


def read_transport_config(path):
    path = Path(path).expanduser().resolve()
    try:
        with path.open(encoding="utf-8") as file:
            config = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid transport camera YAML: {exc}") from exc
    if not isinstance(config, dict) or set(config) - {"launch", "arguments", "cameras"}:
        raise ValueError("Transport camera config must contain cameras and optional launch/arguments")
    cameras = config.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("Transport camera config must list at least one camera")
    if config.get("arguments") and not config.get("launch"):
        raise ValueError("Put arguments on each camera when using individual launches")
    return path, config


def _launch_path(value, directory, share):
    if not isinstance(value, str) or not value.endswith(".launch.py"):
        raise ValueError("Specify a ROS 2 .launch.py file for each camera or for the whole test")
    path = Path(value).expanduser()
    if not path.is_absolute():
        local = directory / path
        path = local if local.is_file() or path.parent != Path(".") else share / "launch" / path
    if not path.is_file():
        raise ValueError(f"Camera Launch file not found: {path}")
    return path.resolve()


def _launch_arguments(values, directory):
    if values is None:
        return {}
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or type(value) not in (str, bool, int, float)
        for key, value in values.items()
    ):
        raise ValueError("Launch arguments must map names to scalar values")
    arguments = {key: str(value).lower() if isinstance(value, bool) else str(value)
                 for key, value in values.items()}
    if arguments.get("config_file_path"):
        local = directory / Path(arguments["config_file_path"]).expanduser()
        if local.is_file():
            arguments["config_file_path"] = str(local.resolve())
    return arguments


def _camera_streams(camera, name, backend, time_domain):
    settings = camera.get("streams")
    if isinstance(settings, list) and all(isinstance(stream, str) for stream in settings):
        if len(set(settings)) != len(settings):
            raise ValueError(f"{name}: streams must be unique")
        settings = dict.fromkeys(settings)
    if not isinstance(settings, dict) or not settings:
        raise ValueError(f"{name}: list the streams that must be measured")
    gpu_topics = camera.get("gpu_topics", {})
    if not isinstance(gpu_topics, dict) or gpu_topics.keys() - settings.keys():
        raise ValueError(f"{name}: gpu_topics must refer to selected streams")
    streams = []
    for stream, specified_topic in settings.items():
        if stream not in STREAM_TOPICS:
            raise ValueError(f"{name}: unknown stream {stream}")
        topic = specified_topic if specified_topic is not None else f"/{name}/{STREAM_TOPICS[stream]}"
        if not isinstance(topic, str) or not topic.startswith("/") or not ROS_NAME.fullmatch(topic[1:]):
            raise ValueError(f"{name}: expected an absolute topic name for {stream}")
        points = stream in POINT_STREAMS
        gpu_topic = topic
        if backend.startswith("NITROS") and points:
            gpu_topic += "/nitros_point_cloud"
        elif backend == "NITROS_GXF":
            gpu_topic += "/nitros_image"
        if stream in gpu_topics:
            override = gpu_topics[stream]
            if not isinstance(override, str) or not override.startswith("/") or not ROS_NAME.fullmatch(override[1:]):
                raise ValueError(f"{name}: expected an absolute GPU topic name for {stream}")
            if backend.startswith("NITROS"):
                gpu_topic = override
        streams.append({
            "camera_name": name, "stream": stream, "topic": topic, "gpu_topic": gpu_topic,
            "kind": "points" if points else "image",
            "host_clock": time_domain in ("global", "system"),
        })
    return streams


def prepare_transport_config(path, share, env, backend, intra_process):
    path, config = read_transport_config(path)
    aggregate = config.get("launch")
    composed = intra_process or backend.startswith("NITROS")
    gpu = backend != "NONE"
    launches = []
    defaults_by_path = {}

    def add_launch(value, arguments, name=None):
        launch = _launch_path(value, path.parent, share)
        if launch not in defaults_by_path:
            defaults_by_path[launch] = camera_launch_options(launch, env)
        defaults = defaults_by_path[launch]
        if composed and not {"component_container_name", "use_intra_process_comms"} <= defaults.keys():
            raise ValueError(f"{launch.name} must forward component_container_name and use_intra_process_comms. "
                             "Use individual compatible camera launches in the configuration.")
        if name is not None:
            if "camera_name" not in defaults:
                raise ValueError(f"{launch.name} does not expose camera_name")
            arguments["camera_name"] = name
        if "use_intra_process_comms" in defaults:
            arguments["use_intra_process_comms"] = str(intra_process).lower()
        if "enable_gpu_transport" in defaults:
            arguments["enable_gpu_transport"] = str(gpu).lower()
        elif gpu and name is not None:
            raise ValueError(f"{launch.name} must expose enable_gpu_transport. "
                             "Use individual compatible camera launches in the configuration.")
        unknown = arguments.keys() - defaults.keys()
        if unknown:
            raise ValueError(f"{launch.name} does not declare: {', '.join(sorted(unknown))}")
        launches.append({"path": str(launch), "arguments": arguments})
        return defaults

    if aggregate:
        add_launch(aggregate, _launch_arguments(config.get("arguments"), path.parent))
    names = set()
    devices = set()
    streams = []
    for camera in config["cameras"]:
        if not isinstance(camera, dict) or set(camera) - {
            "name", "launch", "arguments", "streams", "time_domain", "gpu_topics",
        }:
            raise ValueError("Each camera must specify name and streams. "
                             "Optional keys are launch, arguments, time_domain and gpu_topics.")
        name = camera.get("name")
        if not isinstance(name, str) or not ROS_NAME.fullmatch(name) or "/" in name or name in names:
            raise ValueError(f"Expected a unique camera namespace, got {name!r}")
        names.add(name)
        if aggregate:
            if camera.get("launch") or camera.get("arguments"):
                raise ValueError(f"{name}: use either the top-level launch or an individual launch")
            time_domain = camera.get("time_domain", "global")
        else:
            arguments = _launch_arguments(camera.get("arguments"), path.parent)
            defaults = add_launch(camera.get("launch"), arguments, name)
            for selector in ("serial_number", "usb_port"):
                value = arguments.get(selector)
                if value:
                    if (selector, value) in devices:
                        raise ValueError(f"Duplicate camera {selector}: {value}")
                    devices.add((selector, value))
            if len(config["cameras"]) > 1 and not any(arguments.get(key) for key in ("serial_number", "usb_port")):
                raise ValueError(f"{name}: specify serial_number or usb_port")
            if "device_num" in defaults:
                arguments.setdefault("device_num", str(len(config["cameras"])))
            if "time_domain" in defaults:
                arguments.setdefault("time_domain", str(camera.get("time_domain", "global")))
            time_domain = arguments.get("time_domain", camera.get("time_domain", "global"))
            if arguments.get("config_file_path"):
                camera_config = Path(arguments["config_file_path"]).expanduser()
                if not camera_config.is_absolute():
                    camera_config = share / "config" / camera_config
                with camera_config.open(encoding="utf-8") as file:
                    overrides = yaml.safe_load(file)
                if not isinstance(overrides, dict):
                    raise ValueError(f"Expected a camera parameter mapping in {camera_config}")
                for key in ("camera_name", "serial_number", "usb_port",
                            "enable_gpu_transport", "use_intra_process_comms"):
                    if key not in overrides or key not in arguments:
                        continue
                    value = overrides[key]
                    text = str(value).lower() if isinstance(value, bool) else str(value)
                    if text != arguments[key]:
                        raise ValueError(f"{name}: {camera_config.name} overrides requested {key}")
                time_domain = overrides.get("time_domain", time_domain)
                arguments["config_file_path"] = str(camera_config.resolve())
        if time_domain not in ("global", "system", "device", "unknown"):
            raise ValueError(f"{name}: invalid time_domain {time_domain!r}")
        streams.extend(_camera_streams(camera, name, backend, time_domain))
    for key in ("topic", "gpu_topic"):
        topics = [stream[key] for stream in streams]
        if len(set(topics)) != len(topics):
            raise ValueError(f"Each selected stream must have a unique {key}")
    return launches, streams
