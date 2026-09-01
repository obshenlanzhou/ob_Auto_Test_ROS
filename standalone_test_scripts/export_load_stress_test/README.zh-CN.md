# JSON 导入导出压测

English: [README.md](README.md)

## 工具介绍

交替加载两份 JSON 参数文件，启动相机 launch，出流后立即保存图像并继续验证稳定性，
通过 service 导出 JSON，只对比 `parameters` 字段，验证导入参数是否生效。

每次压测的典型流程：

```text
加载 JSON → 启动各相机 launch
所有流开始出流后立即保存图像 → 继续验证出流稳定性
通过 service 导出 JSON → 对比 parameters 字段
关闭 launch → 切换到下一份 JSON → 重复
```

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera name=camera \
  --run-count 10
```

### 多相机

```bash
python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera name=camera_01,usb-port=2-1 \
  --camera name=camera_02,usb-port=2-3 \
  --run-count 10
```

`--camera` 使用逗号分隔的 `KEY=VALUE` 格式。支持 `name`、`serial-number`、
`usb-port`、`device-ip`、`device-port`、`config-file-path`，每个字段均可选。

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` 或空 | Orbbec 驱动环境 setup 脚本路径 |
| `--launch-file` | `gemini_330_series_sdk_json.launch.py` | launch 文件名或路径 |
| `--camera` | 空 | 相机配置，可重复传入；格式见上文 |
| `--launch-arg` | — | 额外的 launch 参数（如 `enable_ir=true`），可重复传入，格式 `KEY=VALUE` 或 `KEY:=VALUE` |
| `--launch-start-interval` | `2` | 各相机 launch 之间的启动间隔秒数（`0` = 所有相机同时启动） |
| `--run-count` | 空 | 导入导出最大轮次数 |
| `--continue-on-failure` | 关闭 | 记录失败并继续下一轮；最终结果仍为失败 |
| `--duration` | 空 | 可选的最长运行时间；任一已配置上限先达到即结束 |
| `--sdk-log-level` | `debug` | Orbbec SDK 日志级别 |
| `--save-image-count` | `1` | 每轮每个 topic 的产物数；图像采集不等待稳定验证完成（`0` = 仅检测） |
| `--skip-image-frames` | `0` | 每轮对每个图像、点云和 IMU topic 分别跳过前 N 条消息，再开始采集 |
| `--image-topic` | 自动发现 | 指定后只监控并保存这些 `Image` 或 `CompressedImage` topic，可重复传入 |
| `--point-cloud-topic` | 首轮自动发现 | 强制要求的 `PointCloud2` topic，可重复传入并支持 `{camera}` |
| `--imu-topic` | 首轮自动发现 | 强制要求的 `Imu` topic，可重复传入并支持 `{camera}` |
| `--config-json` | 见配置文件 | 交替使用的 JSON 文件，可重复传入 |

`--run-count` 和 `--duration` 至少传入一个，也可以同时传入；同时传入时，任一条件先达到即结束。

默认自动发现每个相机命名空间下所有已发布的 `sensor_msgs/Image` 图像流。
重复传入 `--image-topic` 后，将只监控并保存显式指定的流：

```bash
--image-topic /{camera}/color/image_raw \
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/ir/image_raw
```

raw 彩色/IR `sensor_msgs/Image` 以像素值无损的 PNG 保存（固定无损压缩级别 1），raw 深度仅保存彩色渲染 PNG；
`sensor_msgs/CompressedImage` 不解码、不校验，直接将消息 `data` 原始字节保存为 `.jpg`。
自动发现只选择 `Image`；压缩话题必须通过 `--image-topic` 显式指定。

首次成功启动时，会自动发现各相机命名空间下的 `PointCloud2` 和 `Imu`
话题并固定为后续轮次的必检基线；显式传入的话题从首轮起强制要求。
点云生成带 RGB 或深度着色的 XY/XZ/YZ 三视图；IMU 每张曲线至少采集
2 秒和 10 条有效消息。`--save-image-count 0` 仍执行出流检测。

### 配置文件

默认交替使用两份内置 JSON：

```text
export_load_stress_test/config/Gemini_336L_1.json
export_load_stress_test/config/Gemini_336L_2.json
```

替换为自定义文件时，重复传入 `--config-json`：

```bash
--config-json /path/to/config_A.json \
--config-json /path/to/config_B.json
```

## 结果文件

每次运行会创建结果目录：

```text
export_load_stress_test/results/YYYYMMDD_HHMMSS_export_load_v2.0.0/
├── summary.md       # 最终结果和每次压测通过/失败状态
├── result.json      # 完整机器可读结果
├── events.jsonl     # 结构化生命周期和进度事件
├── images/          # raw 彩色/IR 为无损 PNG，raw 深度为彩色渲染 PNG；CompressedImage 原始字节为 JPG
│   ├── camera_01/color/image_0001.png
│   ├── camera_01/color/image_0002.jpg
│   ├── camera_01/depth/image_0001.png
│   ├── camera_01/point_cloud_depth/point_cloud_0001.ply
│   ├── camera_01/imu_accel/image_0001.png
│   └── camera_02/ir_left/image_0001.png
├── exports/         # 每次压测/每台相机的导出 JSON 和失败 diff
├── logs/test_XXXX/<camera>/<camera>.launch.log  # 每轮 ROS launch 日志
└── logs/test_XXXX/<camera>/sdk/Log/<camera>/  # 每轮相机 SDK debug 日志
```

每台相机的每个流独立编号，并在后续压测轮次继续递增；已有图片不会被覆盖。
