# launch 参数加载压测

English: [README.md](README.md)

## 工具介绍

用于验证每次启动 launch 时，通过 `config_file_path` 传入的 YAML 配置是否生效。
支持多次重复压测和多相机场景。

每次 launch 周期从三个层面验证：

```text
ROS 参数       — 批量查询并与配置 YAML 对比
图像 topic     — 出流开关类参数（enable_color、enable_depth 等）
                通过是否收到图像消息来验证
Getter service — 曝光、增益、白平衡、激光、LDP、PTP、点云降采样
                通过 service 读回设备真实状态验证
```

**局限性**：只有部分参数（见 [VERIFICATION.zh-CN.md](VERIFICATION.zh-CN.md)）支持通过
getter service 读取设备实际状态来判断是否生效。其他参数仅验证 ROS 参数服务器中的值
是否已更新，且 launch 启动过程无报错，则判定加载成功。

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,config-file-path=./config/sample_config_file_path.yaml \
  --run-count 20
```

ROS 1：

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --camera name=camera,config-file-path=./config/sample_config_file_path.yaml \
  --run-count 20
```

### 多相机

多次传入 `--camera` 指定每台设备：

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera1,usb-port=2-1,config-file-path=./config/cam1.yaml \
  --camera name=camera2,usb-port=2-2,config-file-path=./config/cam2.yaml \
  --run-count 10
```

`--camera` 使用逗号分隔的 `KEY=VALUE` 格式。支持 `name`、`serial-number`、
`usb-port`、`device-ip`、`device-port`、`config-file-path`，每个字段均可选。
本脚本要求每个相机配置都包含 `config-file-path`。

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_DRIVER_SETUP`、`$ORBBEC_CAMERA_SETUP` 或空 | Orbbec 驱动环境 setup 脚本路径 |
| `--launch-file` | 必填 | launch 文件名或路径 |
| `--camera` | 必填 | 包含 `config-file-path` 的相机配置，可重复传入 |
| `--launch-arg` | — | 额外的 launch 参数（如 `enable_depth=true`），可重复传入，格式 `KEY=VALUE` 或 `KEY:=VALUE` |
| `--launch-start-interval SECS` | `2` | 各相机 launch 之间的启动间隔秒数（`0` = 所有相机同时启动） |
| `--sdk-log-level` | `debug` | SDK 文件日志级别，可选 `debug/info/warn/error/fatal/none` |
| `--run-count N` | 空 | 完整启动→检查→停止的最大循环次数 |
| `--continue-on-failure` | 关闭 | 记录失败并继续下一轮；最终结果仍为失败 |
| `--duration` | 空 | 可选的最长运行时间；任一已配置上限先达到即结束 |
| `--startup-timeout SECS` | `30` | 等待设备初始化完成的最大秒数 |
| `--topic-timeout SECS` | `20` | 等待每个已启用流 topic 的最大秒数 |
| `--service-timeout SECS` | `15` | 每次参数/service 查询的最大秒数 |
| `--save-image-count N` | `1` | 每个图像、点云和 IMU topic 的产物数；图像采集完成后再执行参数、topic 和 service 检查（`0` = 仅检测） |
| `--skip-image-frames N` | `0` | 每轮对每个图像、点云和 IMU topic 分别跳过前 N 条消息，再开始采集 |
| `--image-topic` | 自动发现 | 指定后只保存这些 `Image` 或 `CompressedImage` topic，可重复传入并支持 `{camera}` |
| `--point-cloud-topic` | 首轮自动发现 | 强制要求的 `PointCloud2` topic，可重复传入并支持 `{camera}` |
| `--imu-topic` | 首轮自动发现 | 强制要求的 `Imu` topic，可重复传入并支持 `{camera}` |
| `--skip-topic-check` | — | 跳过图像 topic 验证 |
| `--skip-service-check` | — | 跳过 getter service 验证 |

`--run-count` 和 `--duration` 至少传入一个，也可以同时传入；同时传入时，任一条件先达到即结束。

默认自动发现每个相机命名空间下所有已发布的 `sensor_msgs/Image` 图像流。
传入一个或多个 `--image-topic` 后，将只保存显式指定的 topic。
多相机时，既可以使用 `{camera}` 模板，也可以分别传入`/camera_01/color/image_raw` 这类完整 topic。
raw 彩色/IR `Image` 以像素值无损的 PNG 保存（固定无损压缩级别 1），raw 深度仅保存彩色渲染 PNG；`CompressedImage`
不解码、不校验，直接将消息 `data` 原始字节保存为 `.jpg`。自动发现不包含压缩话题。

首轮还会自动发现各相机命名空间下的 `PointCloud2` 和 `Imu` 话题，
固定为后续轮次的必检基线。点云生成三视图 PNG，IMU 根据 accel、gyro
或同步话题生成自适应曲线；显式传入的话题从首轮起强制要求。

### 配置文件

配置 YAML 指定需要加载并验证的参数值。内置示例文件：

```text
launch_param_load_stress/config/sample_config_file_path.yaml
```

复制并修改后传入：

```bash
cp ./config/sample_config_file_path.yaml /tmp/my_config.yaml
# 按需修改值后通过相机配置中的 config-file-path 传入
```

占位值（`-1`、空字符串、`ANY`、`none`、`null`）在 service 检查层会被跳过，
驱动会使用设备默认值。

## 结果文件

每次运行会创建结果目录：

```text
launch_param_load_stress/results/YYYYMMDD_HHMMSS_launch_param_load_stress_v2.0.0/
├── test_0001/
│   ├── camera1.launch.log      # camera1 的 ROS launch 日志
│   ├── camera2.launch.log      # camera2 的 ROS launch 日志（多相机时）
│   └── sdk/Log/camera1/
│       └── camera1.log         # 本轮 camera1 的 Orbbec SDK 日志
├── test_0002/
│   └── ...
├── images/                # 仅在 --save-image-count > 0 时生成
│   ├── camera_01/color/image_0001.png
│   ├── camera_01/color/image_0002.jpg
│   ├── camera_01/depth/image_0001.png
│   ├── camera_01/point_cloud_depth/point_cloud_0001.ply
│   ├── camera_01/imu_gyro_accel/image_0001.png
│   └── camera_02/ir_left/image_0001.png
├── summary.md             # 每轮通过/失败汇总
├── events.jsonl           # 结构化生命周期和进度事件
└── result.json            # 所有轮次的机器可读结果
```

每台相机的每个流独立编号，并在后续压测轮次继续递增；已有图片不会被覆盖。
