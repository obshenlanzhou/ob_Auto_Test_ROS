# Image Receive Stats Test

中文文档: [README.zh-CN.md](README.zh-CN.md)

`image_topic_receive_stats.py` subscribes one or more `sensor_msgs/Image`
topics and records subscriber-side receive statistics. It is useful for
checking stream stalls, long receive gaps, timestamp jumps, sequence gaps, and
image metadata changes during long-running ROS tests.

The same script supports ROS1 and ROS2. It detects the runtime from the sourced
ROS environment.

## Environment

Source ROS before running the script:

```bash
source /opt/ros/humble/setup.bash
```

or:

```bash
source /opt/ros/one/setup.bash
```

Start the camera driver separately, then run this subscriber-side monitor.

## Usage

ROS1 example:

```bash
python3 ./image_topic_receive_stats.py \
  --topics "/camera_01/color/image_raw,/camera_01/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --warning_interval_sec 1.0 \
  --warmup_sec 2.0 \
  --queue_size 10 \
  --buff_size 16
```

ROS2 example:

```bash
python3 ./image_topic_receive_stats.py \
  --topics "/camera/color/image_raw,/camera/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --warning_interval_sec 1.0 \
  --warmup_sec 2.0 \
  --queue_size 10 \
  --qos sensor_data
```

## Options

| Option | Description | Default |
| --- | --- | --- |
| `--topics` | Required comma-separated `sensor_msgs/Image` topics | None |
| `--output_dir` | Output directory | `image_receive_stats_<timestamp>` for ROS1, `image_receive_stats_ros2_<timestamp>` for ROS2 |
| `--warning_interval_sec` | Warn when consecutive receive gaps exceed this value | `1.0` |
| `--warmup_sec` | Ignore frames during startup warmup | `2.0` |
| `--queue_size` | Subscriber queue size | `10` |
| `--buff_size` | ROS1 socket buffer size in MB | `16` |
| `--qos` | ROS2 subscriber QoS: `sensor_data`, `default`, `reliable`, or `best_effort` | `sensor_data` |
| `--save_csv` | Enable or disable per-frame CSV saving | `true` |
| `--disable_csv` | Disable per-frame CSV while keeping summary and warnings | Disabled |

## Outputs

The output directory contains:

```text
warnings.log
summary.csv
metadata.json
<topic_name>.csv
```

`summary.csv`, `warnings.log`, and `metadata.json` are always generated.
Per-topic CSV files are generated when per-frame CSV saving is enabled.
