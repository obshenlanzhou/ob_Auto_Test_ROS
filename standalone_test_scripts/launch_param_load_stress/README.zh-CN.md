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
| `--run-count N` | `1` | 完整启动→检查→停止的最大循环次数 |
| `--duration` | 空 | 可选的最长运行时间；任一已配置上限先达到即结束 |
| `--startup-timeout SECS` | `30` | 等待设备初始化完成的最大秒数 |
| `--topic-timeout SECS` | `20` | 等待每个已启用流 topic 的最大秒数 |
| `--service-timeout SECS` | `15` | 每次参数/service 查询的最大秒数 |
| `--save-image-count N` | `1` | 每台相机每个选中流保存的图片数（`0` = 不存图） |
| `--image-topic` | 自动发现 | 指定后只保存这些 topic，可重复传入并支持 `{camera}` |
| `--jpg-quality Q` | `80` | 保存图片的 JPEG 压缩质量（1–100） |
| `--skip-topic-check` | — | 跳过图像 topic 验证 |
| `--skip-service-check` | — | 跳过 getter service 验证 |

默认自动发现每个相机命名空间下所有已发布的 `sensor_msgs/Image` 图像流。
传入一个或多个 `--image-topic` 后，将只保存显式指定的 topic。

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
launch_param_load_stress/results/YYYYMMDD_HHMMSS_launch_param_load_stress/
├── test_0001/
│   ├── camera1.launch.log      # camera1 的 ROS launch 日志
│   ├── camera2.launch.log      # camera2 的 ROS launch 日志（多相机时）
│   └── sdk/Log/camera1/
│       └── camera1.log         # 本轮 camera1 的 Orbbec SDK 日志
├── test_0002/
│   └── ...
├── images/                # 仅在 --save-image-count > 0 时生成
│   ├── camera_01/color/image_0001.jpg
│   ├── camera_01/depth/image_0001.jpg
│   ├── camera_02/ir_left/image_0001.jpg
│   └── camera_02/ir_right/image_0001.jpg
├── summary.md             # 每轮通过/失败汇总
├── events.jsonl           # 结构化生命周期和进度事件
└── result.json            # 所有轮次的机器可读结果
```

每台相机的每个流独立编号，并在后续压测轮次继续递增；已有图片不会被覆盖。
