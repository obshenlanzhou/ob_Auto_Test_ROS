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

> **Note:** The tool cannot configure the current multi-camera launch. Before running, edit each
> camera's name, serial number/USB port, `log_level`, and `log_file_name` in the launch file.

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
| `--ros-version` | `$ROS_VERSION` or `2` | ROS version, either `1` or `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` or empty | Path to the ROS environment setup script |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` or empty | Path to the Orbbec driver environment setup script |
| `--camera-model` | empty | Camera model with a built-in default launch, such as `gemini_301` |
| `--launch-file` | empty | Launch filename or path; required unless a built-in `--camera-model` is used |
| `--image-topic` | auto-discovered | Image topic to monitor; repeatable |
| `--launch-arg` | — | Extra launch argument (e.g. `enable_ir=true`); repeatable, format `KEY=VALUE` or `KEY:=VALUE` |
| `--sdk-log-level` | `debug` | Orbbec SDK log level |
| `--duration` | `300` | Total run time; supports seconds, `30m`, `2h`, and similar formats |
| `--stable-seconds` | `5` | Continuous receive time required for a stream to be considered stable |
| `--stream-timeout` | `20` | Seconds to wait for a stream to become stable per restart |
| `--max-gap-seconds` | `1.5` | Maximum allowed gap between consecutive frames |
| `--restart-delay` | `2` | Seconds to wait between stop and next start |

## Result Files

Each run creates:

```text
launch_restart_stream_check/results/YYYYMMDD_HHMMSS_restart_stream/
├── logs/test_XXXX/<camera>.launch.log      # ROS launch log for each restart
├── logs/test_XXXX/sdk/Log/<camera>/        # SDK debug log for each restart
├── summary.md                              # Run command, final result, elapsed time, monitored streams
├── events.jsonl                            # Structured lifecycle and progress events
└── result.json                             # Structured per-restart results and log paths
```
