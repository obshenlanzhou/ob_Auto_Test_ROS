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
  --ros-version 1 \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_01/depth/image_raw \
  --results-dir ./image_receive_stats_test \
  --warning-interval-sec 1.0 \
  --warmup-sec 2.0 \
  --queue-size 10
```

ROS2 example:

```bash
python3 ./image_topic_receive_stats.py \
  --ros-version 2 \
  --image-topic /camera/color/image_raw \
  --image-topic /camera/depth/image_raw \
  --results-dir ./image_receive_stats_test \
  --warning-interval-sec 1.0 \
  --warmup-sec 2.0 \
  --queue-size 10 \
  --qos sensor_data
```

## Options

| Option | Description | Default |
| --- | --- | --- |
| `--image-topic` | Required `sensor_msgs/Image` topic; repeatable | None |
| `--results-dir` | Output directory | Timestamped directory |
| `--warning-interval-sec` | Warn when consecutive receive gaps exceed this value | `1.0` |
| `--warmup-sec` | Ignore frames during startup warmup | `2.0` |
| `--queue-size` | Subscriber queue size | `10` |
| `--duration` | Optional maximum wall time | Empty |
| `--qos` | ROS2 subscriber QoS: `sensor_data`, `default`, `reliable`, or `best_effort` | `sensor_data` |
| `--save-csv` | Enable or disable per-frame CSV saving | `true` |

## Outputs

The output directory contains:

```text
warnings.log
summary.csv
metadata.json
summary.md
result.json
events.jsonl
<topic_name>.csv
```

The common result files and the statistics summary are always generated.
Per-topic CSV files are generated when per-frame CSV saving is enabled.
