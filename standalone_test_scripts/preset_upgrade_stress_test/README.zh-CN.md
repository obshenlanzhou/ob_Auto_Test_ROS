# Preset 升级压测

English: [README.md](README.md)

## 工具介绍

交替升级两份 optional depth preset bin 文件，每次升级成功后启动相机 launch 并传入
对应 `device_preset`，验证图像流是否稳定。可选择保存图像。

每次升级的典型流程：

```text
升级 preset bin → 启动 launch 并传入对应 device_preset
等待日志出现 "Loaded device preset:"
订阅图像 topic → 验证流稳定 → 保存图像
关闭 launch → 等待 `--restart-delay`（默认 2 秒）→ 切换到下一份 preset → 重复
```

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --run-count 10 \
  --save-image-count 1
```

ROS 1：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --run-count 10
```

### 多相机

多次传入 `--camera`，建议配置 `usb-port` 或 `serial-number` 以避免选错设备：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --camera name=camera_01,usb-port=2-1 \
  --camera name=camera_02,usb-port=2-3 \
  --run-count 10 \
  --save-image-count 1
```

`--camera` 使用逗号分隔的 `KEY=VALUE` 格式。支持 `name`、`serial-number`、
`usb-port`、`device-ip`、`device-port`、`config-file-path`，每个字段均可选。

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` 或空 | Orbbec 驱动环境 setup 脚本路径 |
| `--camera` | 空 | 相机配置，可重复传入；格式见上文 |
| `--preset-a-path` / `--preset-b-path` | 内置 bin 文件 | 交替升级的 preset 文件路径 |
| `--preset-a-name` | `K High Confidence` | preset A 对应的 `device_preset` 名称 |
| `--preset-b-name` | `K High Accuracy` | preset B 对应的 `device_preset` 名称 |
| `--run-count` | 空 | 可选的升级最大轮次数 |
| `--continue-on-failure` | 关闭 | 清理失败的 Preset 测试后继续下一个；最终结果仍为失败 |
| `--duration` | 空 | 最长运行时间，支持 `300`、`15m`、`2h` |
| `--save-image-count` | `1` | 每轮每个图像、点云和 IMU topic 的 PNG 产物数（`0` = 仅检测） |
| `--image-topic` | 自动发现 | 指定后只监控并保存这些 `Image` 或 `CompressedImage` topic，可重复传入并支持 `{camera}` |
| `--point-cloud-topic` | 首轮自动发现 | 强制要求的 `PointCloud2` topic，可重复传入并支持 `{camera}` |
| `--imu-topic` | 首轮自动发现 | 强制要求的 `Imu` topic，可重复传入并支持 `{camera}` |
| `--launch-arg` | — | 额外 launch 参数（如 `enable_left_ir=true`），可重复传入 |
| `--launch-start-interval` | `2` | 各相机 launch 之间的启动间隔秒数（`0` = 所有相机同时启动） |
| `--restart-delay` | `2` | launch 关闭后、切换到下一份 preset 前的等待秒数（`0` = 不额外等待） |
| `--sdk-log-level` | `debug` | preset 升级工具和相机 launch 的 SDK 日志级别 |

`--run-count` 和 `--duration` 至少传入一个，也可以同时传入；同时传入时，任一条件先达到即结束。

默认自动发现每个相机命名空间下所有已发布的 `sensor_msgs/Image` 图像流。
需要限制存图范围时，同时传入 `--image-topic` 和对应的 `--launch-arg`：

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw \
--launch-arg enable_left_ir=true \
--launch-arg enable_right_ir=true
```

`sensor_msgs/Image` 以像素值无损的 PNG 保存（固定无损压缩级别 1），16 位深度值保持不变；
`sensor_msgs/CompressedImage` 不解码、不校验，直接将消息 `data` 原始字节保存为 `.jpg`。
自动发现只选择 `Image`，压缩话题必须显式指定。

首次成功的 preset 测试会发现 `PointCloud2` 和 `Imu` 话题，并固定为
后续测试的必检基线。点云保存 RGB/深度着色三视图，IMU 每张图至少包含
2 秒和 10 条有效消息；显式传入的话题从首轮起强制要求。

### 配置文件

脚本交替使用 preset A 和 preset B。默认映射：

```text
config/g336x_K_High_Confidence_0.0.2.bin → device_preset: K High Confidence
config/g336x_K_High_Accuracy_0.0.2.bin   → device_preset: K High Accuracy
```

替换为自定义文件时，显式传入路径和名称：

```bash
--preset-a-path /path/to/a.bin --preset-a-name "K Clean Medium Confidence" \
--preset-b-path /path/to/b.bin --preset-b-name "K High Accuracy"
```

## 结果文件

每次运行会创建结果目录：

```text
preset_upgrade_stress_test/results/YYYYMMDD_HHMMSS_preset_upgrade/
├── summary.md                              # 最终摘要
├── result.json                             # 完整机器可读结果
├── events.jsonl                            # 结构化生命周期和进度事件
├── logs/test_XXXX/<camera>/upgrade.log     # firmware_update_tool 输出
├── logs/test_XXXX/<camera>/<camera>.launch.log  # launch 输出
├── logs/test_XXXX/<camera>/sdk/Log/         # 升级工具及相机 SDK debug 日志
└── images/                                # raw PNG 和 CompressedImage 原始 JPG
    ├── camera_01/color/image_0001.png
    ├── camera_01/color/image_0002.jpg
    ├── camera_01/depth/image_0001.png
    ├── camera_01/point_cloud_depth/image_0001.png
    ├── camera_01/imu_gyro_accel/image_0001.png
    └── camera_02/ir_left/image_0001.png
```

每台相机的每个流独立编号，并在后续压测轮次继续递增；已有图片不会被覆盖。
