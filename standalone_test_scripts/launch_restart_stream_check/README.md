# Launch Restart Stream Check

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Introduction

Repeatedly restart a ROS launch file and verify that every image stream recovers
and remains stable after each restart.

Typical flow per restart:

```text
Start launch → discover or subscribe to image topics
Wait for all streams to become stable
Stop launch → wait restart delay → repeat until duration ends
```

## Usage

### Single Camera

```bash
cd standalone_test_scripts

python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --duration 1h
```

ROS 1:

```bash
python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --duration 1h
```

If `--image-topic` is not given, the script auto-discovers all
`sensor_msgs/Image` topics during the first launch and monitors the same list
in later restarts.

### Multi-Camera

Pass `--image-topic` once per topic to monitor, or omit it to auto-discover all
streams from a multi-camera launch file:

```bash
python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file multi_camera.launch.py \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_01/depth/image_raw \
  --image-topic /camera_02/color/image_raw \
  --image-topic /camera_02/depth/image_raw \
  --duration 1h
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--duration` | `1h` | Total run time (e.g. `30m`, `2h`) |
| `--stable-seconds` | `5` | Continuous receive time required for a stream to be considered stable |
| `--stream-timeout` | `20` | Seconds to wait for a stream to become stable per restart |
| `--max-gap-seconds` | `1.5` | Maximum allowed gap between consecutive frames |
| `--restart-delay` | `2` | Seconds to wait between stop and next start |

## Result Files

Each run creates:

```text
launch_restart_stream_check/results/YYYYMMDD_HHMMSS_restart_stream/
└── summary.md     # Run command, final result, elapsed time, monitored streams
```
