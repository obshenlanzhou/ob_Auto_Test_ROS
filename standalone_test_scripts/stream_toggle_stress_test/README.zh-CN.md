# 逐路开关流压测

English: [README.md](README.md)

## 工具介绍

工具启动一个 ROS launch，并通过驱动提供的 `toggle_<stream>` 服务逐路关闭和恢复
图像流。支持 ROS 1、ROS 2、单相机和预配置的多相机 launch。

每路流的验证事务为：

```text
关闭目标流
  → 目标流连续 2 秒无图像，同时全部其他目标流连续稳定 5 秒
  → 重新开启目标流
  → 全部目标流连续稳定 5 秒
  → 保存当前目标流图像
```

驱动开关一路流时会重建相机 pipeline，因此工具会监控多相机 launch 中全部选中流，
检查同一相机及其他相机是否受到连带影响。执行顺序固定为相机命名空间、流名排序，
一次只操作一路流。

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,usb-port=2-1 \
  --duration 1h
```

ROS 1：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --camera name=camera,usb-port=2-1 \
  --run-count 10
```

单相机可传入最多一个 `--camera`，字段格式与其他独立脚本相同：`name`、
`serial-number`、`usb-port`、`device-ip`、`device-port` 和 `config-file-path`。
这些字段会转换为 launch 参数。未传 `--camera` 时，不注入相机专属参数，launch 使用
自身默认值。

### 多相机 launch

多相机场景使用一份已配置好相机名称、SN/USB port、流开关、SDK 日志级别和日志文件名的 launch。
工具只启动一次该 launch，不要重复传入 `--camera`：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file /path/to/multi_camera.launch.py \
  --duration 1h
```

默认自动发现所有 `sensor_msgs/Image` topic，并只选择存在对应
`std_srvs/SetBool` 服务的流。例如：

```text
/camera_01/color/image_raw  → /camera_01/toggle_color
/camera_02/left_ir/image_raw → /camera_02/toggle_left_ir
```

`depth_to_color`、`confidence` 等没有对应 toggle 服务的派生 Image topic 会被跳过并写入
报告。也可以重复传入 `--image-topic`，严格指定必须出现和测试的流：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --launch-file /path/to/multi_camera.launch.py \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_01/depth/image_raw \
  --image-topic /camera_02/color/image_raw \
  --image-topic /camera_02/depth/image_raw \
  --run-count 10
```

显式 topic 必须是标准的 `/<camera-namespace>/<stream>/image_raw`，类型必须为
`sensor_msgs/Image`，且对应 `toggle_<stream>` 服务必须存在，否则前置检查失败。
`{camera}` 占位符仅在传入单个 `--camera` 时可用。

## 循环、重试与停止

一个完整循环表示所有目标流各完成一次“关闭→验证→开启→验证→存图”。同时设置
`--run-count` 和 `--duration` 时，任一上限先达到即停止。首个完整循环始终执行完，保证
每个目标流至少验证一次；后续到达时间上限时，会在当前单路事务完成恢复后停止。

服务调用默认超时 15 秒。首次调用失败时等待 1 秒重试一次：

- 重试成功：继续运行，最终仍可为 `passed`，但在 warnings 和步骤明细中记录。
- 两次失败：立即失败，尽力重新开启当前流，然后停止 launch。
- Ctrl+C/UI 停止：尽力恢复当前流并短暂确认出流，状态为 `interrupted`，退出码 130。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，`1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本 |
| `--driver-setup` | `$ORBBEC_DRIVER_SETUP` / `$ORBBEC_CAMERA_SETUP` 或空 | 驱动环境 setup 脚本 |
| `--launch-package` | `orbbec_camera` | launch 包名 |
| `--launch-file` | 必填 | launch 文件名或路径 |
| `--launch-arg` | — | 附加 launch 参数，可重复传入 |
| `--camera` | 空 | 单相机 launch 参数，最多一个 |
| `--image-topic` | 自动发现 | 严格指定目标原始图像流，可重复传入 |
| `--duration` | `300` | 最长运行时间，支持 `15m`、`2h` |
| `--run-count` | 空 | 最大完整循环数 |
| `--topic-discovery-timeout` | `30` | 话题/服务发现最长等待时间 |
| `--topic-discovery-settle` | `2` | 自动发现无新增目标的静默窗口 |
| `--stop-stable-seconds` | `2` | 目标流无图像的停流确认窗口 |
| `--stable-seconds` | `5` | 恢复后连续稳定出流时间 |
| `--stream-timeout` | `20` | 每个关闭/开启状态验证超时 |
| `--max-gap-seconds` | `1.5` | 稳定窗口内最大帧接收间隔 |
| `--service-timeout` | `15` | 单次 toggle 服务调用超时 |
| `--service-retry-delay` | `1` | 首次服务失败后的重试等待时间 |
| `--save-image-count` | `1` | 每路每循环保存 JPG 数量，`0` 为关闭 |
| `--save-image-timeout` | `30` | 每路存图最长等待时间 |
| `--jpg-quality` | `95` | JPG 质量，1–100 |
| `--sdk-log-level` | `debug` | 单相机 launch 的 SDK 日志级别；多相机由 launch 预配置 |
| `--queue-size` | `10` | 图像订阅队列大小 |
| `--results-dir` | 自动生成 | 自定义结果目录 |

启用存图需要 `cv_bridge` 和 OpenCV；脚本会在启动 launch 前检查依赖。可通过
`--save-image-count 0` 禁用存图。

## 结果文件

```text
stream_toggle_stress_test/results/YYYYMMDD_HHMMSS_stream_toggle/
├── logs/
│   ├── camera.launch.log       # 整次运行的连续 launch 日志
│   └── sdk/                    # Orbbec SDK 日志目录
├── images/
│   ├── camera_01/color/image_0001.jpg
│   ├── camera_01/depth/image_0001.jpg
│   └── camera_02/color/image_0001.jpg
├── summary.md
├── events.jsonl
└── result.json
```

`result.json` 记录每个循环、每路服务调用及重试、停流和恢复耗时、帧统计、最大帧间隔、
恢复清理和图片路径。状态与退出码遵循统一独立脚本契约：`passed`/0、`failed`/1、
`interrupted`/130；命令行参数错误为 2。
