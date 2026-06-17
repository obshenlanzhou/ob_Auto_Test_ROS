# launch 参数加载压测

English: [README.md](README.md)

该独立脚本通过 `config_file_path` 传入 YAML 配置文件启动 Orbbec ROS 相机驱动，
验证配置是否正确加载到驱动中，并支持多次重复测试（压测）。支持单相机和多相机场景，
兼容 ROS 1 和 ROS 2，不依赖 `orbbec_camera_auto_test` 自动化测试框架。

每次 launch 周期从三个层面验证配置是否生效：

- **ROS 参数值** — 通过 `ros2 param dump` / `rosparam get` 批量查询参数
- **图像流 topic** — 出流开关类参数（`enable_color`、`enable_depth` 等）通过是否收到图像消息来验证
- **只读 getter service** — 曝光、增益、白平衡、激光、LDP、PTP、点云降采样等参数通过 service 读回真实设备状态验证

启动就绪判断：监控 launch 日志，检测到 `Initialize device cost:` 后才开始检查。
日志中出现 `[ERROR]`、`[WARN]`、`[FATAL]` 或 Python `Traceback` 时，立即终止当前 run。

## 快速开始

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/orbbec_camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --config-file-path ./config/sample_config_file_path.yaml
```

ROS 1：

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/orbbec_camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --config-file-path ./config/sample_config_file_path.yaml
```

## 压测（多次重复）

使用 `--repeat N` 连续启动并验证 N 次，每次 run 均独立启动、检查、停止 launch 进程。

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --config-file-path ./config/sample_config_file_path.yaml \
  --repeat 20
```

脚本会打印每次 run 的通过/失败情况，只有全部 run 均通过才返回 `0`。

## 多相机

多相机模式下，每台相机独立启动一个 launch 进程，各自有独立日志。
使用 `--camera` 参数，每台相机指定一次。多相机时每台设备必须提供 `usb_port` 以区分设备。

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera1,usb_port=2-1,config_file_path=./config/cam1.yaml \
  --camera camera2,usb_port=2-2,config_file_path=./config/cam2.yaml
```

`--camera` 格式：

```text
name[,serial_number=SN][,usb_port=X][,config_file_path=/path/to/config.yaml]
```

也可以用 `--config-file-path` 指定一个公用配置，未在 `--camera` 中指定
`config_file_path` 的相机将使用该公用配置。

## 多相机压测

`--camera` 和 `--repeat` 可同时使用：

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera1,usb_port=2-1,config_file_path=./config/cam1.yaml \
  --camera camera2,usb_port=2-2,config_file_path=./config/cam2.yaml \
  --repeat 10
```

## 配置 YAML

示例文件：[`config/sample_config_file_path.yaml`](config/sample_config_file_path.yaml)。

请填写当前设备支持的有效值。`-1`、空字符串、`ANY`、`none`、`null` 等占位值
在 service 检查层会被跳过（驱动可能使用设备默认值或自动选择）。

```bash
cp ./config/sample_config_file_path.yaml /tmp/my_test_config.yaml
# 按需修改值后通过 --config-file-path 传入
```

## 参数说明

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version {1,2}` | `2` | ROS 版本 |
| `--ros-setup PATH` | | ROS 环境 setup 脚本（如 `/opt/ros/humble/setup.bash`） |
| `--driver-setup PATH` | | 相机驱动工作空间 setup 脚本 |
| `--launch-package PKG` | `orbbec_camera` | 包含 launch 文件的 ROS 包 |
| `--launch-file FILE` | | launch 文件名或绝对路径（必填） |
| `--config-file-path PATH` | | 公用配置 YAML（未使用 `--camera` 时必填） |
| `--camera SPEC` | | 单台相机规格，可重复传入实现多相机 |
| `--camera-name NAME` | `camera` | 单相机模式下的相机名 |
| `--serial-number SN` | | 单相机模式下的序列号 |
| `--usb-port PORT` | | 单相机模式下的 USB 口 |
| `--launch-arg KEY=VALUE` | | 额外 launch 参数，可重复传入 |
| `--startup-timeout SECS` | `30` | 等待 `Initialize device cost:` 的最大秒数 |
| `--topic-timeout SECS` | `20` | 等待每个已启用流 topic 的最大秒数 |
| `--service-timeout SECS` | `15` | 每次参数/service 查询的超时秒数 |
| `--repeat N` | `1` | 完整测试周期的重复次数（压测） |
| `--results-dir PATH` | | 结果输出目录，默认为 `results/<run_id>/` |
| `--skip-topic-check` | | 跳过图像 topic 验证 |
| `--skip-service-check` | | 跳过 getter service 验证 |
| `--save-images-count N` | `0` | 每台相机每个已启用流保存 N 张图（`0` = 不保存） |
| `--jpg-quality Q` | `80` | 保存图片的 JPEG 压缩质量（1–100） |
| `--show-verification-map` | | 打印内置验证映射并退出 |

## 启动就绪检测

脚本监控 launch 日志文件，检测到 `Initialize device cost:` 后才开始各项验证，
避免在驱动未就绪时查询失败。

日志中若出现 `[ERROR]`、`[WARN]`、`[FATAL]`、`process has died` 或
Python `Traceback` 等行，当前 run 立即终止并标记为失败。

## 验证规则

详细映射见：[VERIFICATION.zh-CN.md](VERIFICATION.zh-CN.md)

- **参数**：优先使用 `ros2 param dump /<camera>/<camera>`（ROS 2）或
  `rosparam get /<camera>/<camera>`（ROS 1）批量获取，批量失败时退回逐个查询。
- **Topic**：`enable_color`、`enable_depth`、`enable_ir`、`enable_left_ir`、
  `enable_right_ir`、`enable_left_color`、`enable_right_color` 通过在
  `--topic-timeout` 内是否收到图像消息来验证。
- **Service**：曝光、增益、白平衡、激光、LDP、PTP、点云降采样在对应 getter
  service 已暴露时读回设备状态验证。

```bash
python3 ./launch_param_load_stress.py --show-verification-map
```

## 输出结构

```text
results/<run_id>/
├── run001/
│   ├── camera1.log        # camera1 的 launch 日志
│   └── camera2.log        # camera2 的 launch 日志（多相机时）
├── run002/
│   └── ...
├── images/                # 仅在 --save-images-count > 0 时生成
│   ├── run001/
│   │   ├── camera1/
│   │   │   ├── _camera1_color_image_raw/
│   │   │   │   ├── image_0001.jpg
│   │   │   │   └── image_0002.jpg
│   │   │   └── _camera1_depth_image_raw/
│   │   │       └── image_0001.jpg
│   │   └── camera2/
│   │       └── ...
│   └── run002/
│       └── ...
├── summary.md             # 所有 run 的可读汇总
└── result.json            # 所有 run 的机器可读结果
```

- 全部 run、全部相机通过：返回 `0`
- 任意失败：返回 `1`
- 用户中断（Ctrl-C）：返回 `130`
