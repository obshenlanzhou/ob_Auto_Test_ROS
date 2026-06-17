# Launch 重启出流检查

English: [README.md](README.md)

## 工具介绍

反复重启一个 ROS launch 文件，检查每次重启后图像流是否能够恢复并稳定出流。

每次重启的典型流程：

```text
启动 launch → 自动发现或使用配置的 image topic
等待所有流稳定出流
关闭 launch → 等待 restart delay → 重复直到 duration 结束
```

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --duration 1h
```

ROS 1：

```bash
python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --duration 1h
```

不传 `--image-topic` 时，脚本会在第一次 launch 启动后自动发现所有
`sensor_msgs/Image` topic，后续重启轮次固定使用该列表。

### 多相机

多次传入 `--image-topic` 指定需要监控的 topic，或者不传让脚本自动发现多相机
launch 文件中的所有流：

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

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--duration` | `1h` | 总运行时长（如 `30m`、`2h`） |
| `--stable-seconds` | `5` | 判定流稳定所需的持续接收时间（秒） |
| `--stream-timeout` | `20` | 每次重启后等待流稳定的最大秒数 |
| `--max-gap-seconds` | `1.5` | 相邻两帧接收间隔的最大容许值（秒） |
| `--restart-delay` | `2` | 关闭 launch 后到下次启动的等待秒数 |

## 结果文件

每次运行会创建结果目录：

```text
launch_restart_stream_check/results/YYYYMMDD_HHMMSS_restart_stream/
└── summary.md     # 运行命令、最终结果、运行时长、监控流列表
```
