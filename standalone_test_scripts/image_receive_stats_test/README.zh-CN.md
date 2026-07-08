# 图像接收统计测试

English: [README.md](README.md)

`image_topic_receive_stats.py` 用于订阅一个或多个 `sensor_msgs/Image` 话题，并记录订阅端接收统计数据。该脚本适合在长时间运行测试中检查停流、接收间隔突增、时间戳跳变、消息序号异常和图像信息变化。

同一个脚本支持 ROS1 和 ROS2，会根据已经 source 的 ROS 环境自动识别运行时。

## 环境

运行脚本前先加载 ROS 环境：

```bash
source /opt/ros/humble/setup.bash
```

或：

```bash
source /opt/ros/one/setup.bash
```

相机驱动需要单独启动，该脚本只做订阅端监测。

## 使用示例

ROS1 示例：

```bash
python3 ./image_topic_receive_stats.py \
  --topics "/camera_01/color/image_raw,/camera_01/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --warning_interval_sec 1.0 \
  --warmup_sec 2.0 \
  --queue_size 10 \
  --buff_size 16
```

ROS2 示例：

```bash
python3 ./image_topic_receive_stats.py \
  --topics "/camera/color/image_raw,/camera/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --warning_interval_sec 1.0 \
  --warmup_sec 2.0 \
  --queue_size 10 \
  --qos sensor_data
```

## 参数说明

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--topics` | 必填，逗号分隔的 `sensor_msgs/Image` 话题列表 | 无 |
| `--output_dir` | 输出目录 | ROS1 为 `image_receive_stats_<timestamp>`，ROS2 为 `image_receive_stats_ros2_<timestamp>` |
| `--warning_interval_sec` | 相邻两帧接收间隔超过该值时记录告警 | `1.0` |
| `--warmup_sec` | 启动预热时间，预热期间不记录统计 | `2.0` |
| `--queue_size` | Subscriber 队列大小 | `10` |
| `--buff_size` | ROS1 Subscriber socket buffer 大小，单位 MB | `16` |
| `--qos` | ROS2 Subscriber QoS，可选 `sensor_data`、`default`、`reliable`、`best_effort` | `sensor_data` |
| `--save_csv` | 是否保存逐帧 CSV | `true` |
| `--disable_csv` | 关闭逐帧 CSV，但保留汇总和告警文件 | 默认关闭 |

## 输出文件

输出目录中包含：

```text
warnings.log
summary.csv
metadata.json
<topic_name>.csv
```

`summary.csv`、`warnings.log` 和 `metadata.json` 始终生成。启用逐帧 CSV 时，每个话题还会生成一个对应的 CSV 文件。
