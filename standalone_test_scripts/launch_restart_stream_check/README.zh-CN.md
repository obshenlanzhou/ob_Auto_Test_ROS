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

> **注意：** 当前多相机 launch 不支持由工具配置。运行前请自行修改其中每台相机的
> name、SN/USB port、`log_level` 和 `log_file_name`。

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
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` 或空 | Orbbec 驱动环境 setup 脚本路径 |
| `--camera-model` | 空 | 使用内置默认 launch 的相机型号，例如 `gemini_301` |
| `--launch-file` | 空 | launch 文件名或路径；未使用内置 `--camera-model` 时必填 |
| `--image-topic` | 自动发现 | 监控的图像 topic，可重复传入 |
| `--point-cloud-topic` | 首次启动自动发现 | 每次重启后必检的 `PointCloud2` topic，可重复传入并支持 `{camera}` |
| `--imu-topic` | 首次启动自动发现 | 每次重启后必检的 `Imu` topic，可重复传入并支持 `{camera}` |
| `--save-image-count` | `1` | 每次重启后每个图像、点云和 IMU topic 的 PNG 数量；`0` 仅检测 |
| `--launch-arg` | — | 额外的 launch 参数（如 `enable_ir=true`），可重复传入，格式 `KEY=VALUE` 或 `KEY:=VALUE` |
| `--sdk-log-level` | `debug` | Orbbec SDK 日志级别 |
| `--duration` | 空 | 总运行时长，支持秒数、`30m`、`2h` 等格式 |
| `--run-count` | 空 | 最大完整重启循环数 |
| `--stable-seconds` | `5` | 判定流稳定所需的持续接收时间（秒） |
| `--stream-timeout` | `20` | 每次重启后等待流稳定的最大秒数 |
| `--max-gap-seconds` | `1.5` | 相邻两帧接收间隔的最大容许值（秒） |
| `--restart-delay` | `2` | 关闭 launch 后到下次启动的等待秒数 |

`--run-count` 和 `--duration` 至少传入一个，也可以同时传入；同时传入时，任一条件先达到即结束。

首次成功启动会固定点云和 IMU 基线，后续每次重启都要求相同话题恢复。
成功重启后保存普通图像、点云三视图和自适应 IMU 曲线；显式传入的话题
从第一次启动起强制要求。

## 结果文件

每次运行会创建结果目录：

```text
launch_restart_stream_check/results/YYYYMMDD_HHMMSS_restart_stream/
├── logs/test_XXXX/<camera>.launch.log      # 每次重启的 ROS launch 日志
├── logs/test_XXXX/sdk/Log/<camera>/        # 每次重启的 SDK debug 日志
├── images/<camera>/<stream>/image_NNNN.png # 普通图像、点云和 IMU 证据
├── summary.md                              # 运行命令、最终结果、运行时长、监控流列表
├── events.jsonl                            # 结构化生命周期和进度事件
└── result.json                             # 每次重启的结构化结果和日志路径
```
