# Orbbec Camera Auto Test

面向 Orbbec ROS 相机的自动化测试工作区，支持 ROS2 和 ROS1，提供命令行与本地 Web UI 两种使用方式。

当前覆盖以下测试能力：

- 功能测试：检查相机节点、Topic、Service、图像/点云保存和设备重启恢复。
- 性能测试：统计图像流 FPS、进程 CPU、内存和进程数，支持高负载、丢帧分析及多相机测试。
- Launch 重启测试：反复启动和停止驱动，验证图像流能否稳定恢复。
- 长时间断流测试：持续监控图像帧间隔，记录异常断流。
- 组合测试：功能测试通过后继续执行性能测试。

## 快速开始

### 环境要求

- ROS2 Humble，或可用的 ROS1 环境。
- 已编译的 `orbbec_camera` 驱动工作区。
- 已连接并能被驱动识别的 Orbbec 相机。
- Python 依赖：`PyYAML`、`psutil`，以及对应 ROS 版本的 `rclpy` 或 `rospy`。
- 使用高负载性能场景时，需要额外安装 `stress-ng`。

进入工作区：

```bash
cd "$HOME/ORBBEC/ob_Auto_Test_ROS/auto_test_ws"
```

### 启动 Web UI

Web UI 是推荐的交互入口，不需要先执行 `colcon build`：

```bash
./run_camera_auto_test_ui.sh
```

浏览器访问：

```text
http://127.0.0.1:8000
```

指定地址和端口：

```bash
./run_camera_auto_test_ui.sh --host 127.0.0.1 --port 8001
```

页面中选择 ROS 版本、相机类型、测试模式和 Profile，填写驱动环境路径后即可运行。

### 运行命令行测试

ROS2 功能测试：

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --profile gemini_330_series \
  --driver-setup /path/to/ros2_driver/install/setup.bash
```

ROS1 功能测试：

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --profile gemini_330_series \
  --ros-version 1 \
  --ros-setup /opt/ros/one/setup.bash \
  --driver-setup /path/to/ros1_driver/devel/setup.bash
```

查看全部命令行参数：

```bash
./run_camera_auto_test.sh --help
```

## 驱动和 ROS 环境

测试脚本会先加载 ROS 环境，再加载 Orbbec 驱动环境。

默认 ROS 环境：

- ROS2：`/opt/ros/humble/setup.bash`
- ROS1：`/opt/ros/one/setup.bash`

可以通过参数覆盖：

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/install/setup.bash
```

也可以使用环境变量：

```bash
export ORBBEC_ROS_VERSION=2
export ORBBEC_ROS_SETUP=/opt/ros/humble/setup.bash
export ORBBEC_DRIVER_SETUP=/path/to/install/setup.bash
./run_camera_auto_test.sh --mode functional
```

Web UI 可分别预置 ROS2 和 ROS1 驱动环境：

```bash
export ORBBEC_ROS2_CAMERA_SETUP=/path/to/ros2_driver/install/setup.bash
export ORBBEC_ROS1_CAMERA_SETUP=/path/to/ros1_driver/devel/setup.bash
./run_camera_auto_test_ui.sh
```

以上 Web UI 环境变量既可以指向 `setup.bash`/`setup.zsh` 文件，也可以指向包含该文件的目录。页面填写的路径优先于环境变量。Shell 入口建议传入 `setup.bash`。


## 测试模式

| 模式 | 命令行脚本 | Web UI | 作用 |
| --- | --- | --- | --- |
| `functional` | 支持 | 支持 | 运行功能场景并检查已发现的 Topic 和 Service |
| `performance` | 支持 | 支持 | 采集 FPS、CPU、内存和进程数据 |
| `restart` | 支持 | 支持 | 反复重启 Launch 并等待图像流稳定 |
| `stream_stall` | 独立 runner | 支持 | 长时间监控图像流断流和帧间隔 |
| `all` | 支持 | 支持 | 功能测试通过后运行性能测试 |

### 功能测试

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --profile gemini_301_double_color \
  --driver-setup /path/to/install/setup.bash
```

功能测试的主要流程：

1. 检测已连接的 Orbbec 设备。
2. 根据 Profile 启动对应 Launch。
3. 等待相机节点和基础 Service 就绪。
4. 从 ROS Graph 获取当前已发布的 Topic 和 Service。
5. 只对“统一接口清单中存在，并且 ROS Graph 已发现”的接口执行检查。
6. 执行可用的只读、回环、保存文件和设备重启检查。
7. 输出 JSON、Markdown 和详细日志。

统一接口清单位于 [all_topics_services.yaml](src/orbbec_camera_auto_test/profiles/base/all_topics_services.yaml)。所有功能 Profile 都继承这份文件：

- ROS Graph 未发现的接口会在预检阶段排除，不会逐项等待超时。
- 已发现的 Topic 如果类型不匹配、无法收到消息或内容校验失败，测试会失败。
- 已发现的 Service 会根据配置执行存在性、读取、回环或副产物检查。


### 性能测试

使用 Profile 中的默认性能场景：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --profile gemini_330_series \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

只运行指定场景：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --profile gemini_330_series \
  --performance-scenario high_performance_launch \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

常见场景包括：

- `default`：按默认流配置采集性能数据。
- `default_with_stress_ng_load`：在 `stress-ng` 高负载下观察性能和丢帧。
- `high_performance_launch`：开启更多或更高规格的数据流。
- `drop_frame_analysis`：同时记录驱动端与接收端时间戳，用于丢帧分析。

具体场景以所选性能 Profile 为准。命令行传入的 `--duration` 会覆盖 Profile 中的场景时长，支持 `300`、`15m`、`2h` 等格式。

性能测试会启动独立的干净 Launch，不复用功能测试进程。当前结果以统计和报告为主：进程异常退出或采集失败会判定失败，但 FPS、CPU、内存暂不设置统一硬阈值。

性能 Topic 同样会经过 ROS Graph 预检，只对已发现的流启动 FPS 采集；如果一个配置的性能 Topic 都没有发现，场景会失败。

### 丢帧分析

Gemini 330 提供专用 Profile：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --profile gemini_330_drop_frame \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

该 Profile 会启用驱动端帧时间戳 CSV，并记录接收端图像时间戳。典型产物包括：

- `fps.csv`
- `driver_frame_timestamp.csv`
- `frame_timestamps/camera_color_image_raw.csv`
- `frame_timestamps/camera_depth_image_raw.csv`

### 多相机性能测试

独立容器模式：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --profile gemini_330_multi_isolated \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

共享容器模式：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --profile gemini_330_multi_shared \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

- `gemini_330_multi_isolated` 对应 `multi_camera.launch.py`，分别统计每台相机及整体资源占用。
- `gemini_330_multi_shared` 对应 `orbbec_multicamera.launch.py`，按相机统计 FPS，CPU 和内存按共享容器整体统计。

实际相机名称必须与 Profile 中的 `multi_camera.cameras` 和 `multi_camera.topic_templates` 一致。

### Launch 重启测试

```bash
./run_camera_auto_test.sh \
  --mode restart \
  --duration 30m \
  --launch-file gemini_330_series.launch.py \
  --image-topic /camera/color/image_raw \
  --stable-seconds 10 \
  --stream-timeout 60 \
  --max-gap-seconds 1.5 \
  --restart-delay 2 \
  --driver-setup /path/to/install/setup.bash
```

每轮启动后，测试会等待指定图像 Topic 连续稳定出流。成功后停止 Launch，等待 `restart-delay`，再开始下一轮。如果在 `stream-timeout` 内无法稳定出流，本轮会记录 `warning`，并保持当前 Launch 运行，供人工继续确认；手动停止 Launch 或中断测试后才会退出。

`--image-topic` 可以重复传入，用于同时监控多个图像流。

### 长时间断流测试

`stream_stall` 目前可以直接在 Web UI 中选择；统一 Shell 脚本暂未暴露该模式。需要命令行运行时，可调用独立 runner：

```bash
PYTHONPATH=src/orbbec_camera_auto_test \
python3 -m orbbec_camera_auto_test.runners.stream_stall \
  --launch-file gemini_330_series.launch.py \
  --results-dir results/stream_stall_manual \
  --duration 1h \
  --image-topic /camera/color/image_raw \
  --driver-setup /path/to/install/setup.bash
```

### 功能与性能组合测试

```bash
./run_camera_auto_test.sh \
  --mode all \
  --profile gemini_330_series \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

`all` 会先运行功能测试；只有功能测试成功，才会继续执行性能测试。Web UI 中可以分别选择功能 Profile 和性能 Profile。

## 相机类型与 Profile

“相机类型”主要用于 Web UI 筛选 Profile，并为重启、断流测试选择默认 Launch 文件。真正决定 Launch 文件、Launch 参数和测试场景的是 Profile。

当前内置 Profile：

| 相机 | 类型 | Profile | 用途 |
| --- | --- | --- | --- |
| Gemini 301 | 功能 | `gemini_301_depth_color_left_right_ir` | 深度、彩色、左右 IR |
| Gemini 301 | 功能 | `gemini_301_double_color` | 左右彩色流 |
| Gemini 301 | 性能 | `gemini_301_depth_color_left_right_ir` | 深度、彩色、左右 IR 性能测试 |
| Gemini 301 | 性能 | `gemini_301_double_color` | 双彩色流性能测试 |
| Gemini 330 | 功能 | `gemini_330_series` | Gemini 330 功能测试 |
| Gemini 330 | 性能 | `gemini_330_series` | 常规性能测试 |
| Gemini 330 | 性能 | `gemini_330_drop_frame` | 驱动端与接收端丢帧分析 |
| Gemini 330 | 性能 | `gemini_330_multi_isolated` | 多相机独立容器测试 |
| Gemini 330 | 性能 | `gemini_330_multi_shared` | 多相机共享容器测试 |

Profile 可以使用名称，也可以直接传入 YAML 路径：

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --profile /path/to/custom_profile.yaml \
  --driver-setup /path/to/install/setup.bash
```

### Profile 目录

```text
src/orbbec_camera_auto_test/profiles/
├── base/                       # 通用接口、功能组合和性能场景
└── cameras/
    ├── gemini_301/
    │   ├── functional/
    │   └── performance/
    └── gemini_330/
        ├── functional/
        └── performance/
```

功能 Profile 负责定义：

- `extends`：继承的基础配置。
- `launch_file`：驱动 Launch 文件。
- `default_launch_args`：默认启动参数。
- `launch_scenarios`：功能测试场景。

性能 Profile 还可以定义：

- `performance_scenarios`：性能场景、时长、启动参数和外部负载。
- `performance_topics`：需要统计的图像流。
- `frame_timestamps`：接收端帧时间戳记录方式。
- `multi_camera`：多相机名称、Topic 模板和资源统计模式。

新增接口时统一修改 [all_topics_services.yaml](src/orbbec_camera_auto_test/profiles/base/all_topics_services.yaml)；新增机型或流组合时，优先通过继承基础配置创建相机专用 Profile，不要复制整份接口清单。

## 常用参数

### 通用参数

| 参数 | 说明 |
| --- | --- |
| `--mode` | `functional`、`performance`、`restart` 或 `all` |
| `--profile NAME_OR_PATH` | Profile 名称或 YAML 文件路径 |
| `--driver-setup PATH` | 驱动工作区环境脚本，Shell 入口建议使用 `setup.bash` |
| `--ros-version 1\|2` | 选择 ROS1 或 ROS2 |
| `--ros-setup PATH` | ROS 环境脚本 |
| `--results-root PATH` | 自定义结果根目录 |
| `--launch-file FILE` | 覆盖 Profile 中的 Launch 文件 |
| `--launch-arg KEY=VALUE` | 覆盖 Launch 参数，可重复传入 |
| `--camera-name NAME` | 覆盖 `camera_name` |
| `--serial-number SERIAL` | 按序列号选择相机 |
| `--usb-port PORT` | 按 USB 端口选择相机 |
| `--config-file-path PATH` | 传入驱动配置文件 |

Launch 参数的优先级为：命令行 `--launch-arg` 和专用参数 > Profile 默认参数 > 驱动 Launch 默认值。

### 性能与重启参数

| 参数 | 说明 |
| --- | --- |
| `--duration DURATION` | 性能或重启测试时长，支持秒数、`m`、`h` |
| `--performance-scenario NAME` | 只运行指定性能场景 |
| `--image-topic TOPIC` | 重启测试监控的 Topic，可重复传入 |
| `--stable-seconds SECONDS` | 每轮需要连续稳定出流的时间 |
| `--stream-timeout SECONDS` | 每轮等待稳定出流的最大时间 |
| `--max-gap-seconds SECONDS` | 稳定出流期间允许的最大帧间隔 |
| `--restart-delay SECONDS` | 两轮 Launch 之间的等待时间 |

## Web UI 行为

Web UI 会根据相机类型筛选可用 Profile，并根据测试模式显示对应配置：

- `functional`：选择功能 Profile。
- `performance`：选择性能 Profile 和性能场景。
- `restart`：配置稳定出流、超时和重启间隔。
- `stream_stall`：配置监控时长、预热、告警间隔和 CSV 输出。
- `all`：分别选择功能 Profile 与性能 Profile。

测试运行期间，页面会显示实时日志；性能测试还会展示已运行时间、CPU、内存、进程数和各 Topic FPS。

最近一次页面配置保存在：

```text
results/ui_config.json
```

UI 测试结果保存在：

```text
results/ui_runs/<run_id>/
```

`results/` 是运行时生成目录，不应提交到 Git。

## 结果目录

Shell 脚本每次运行会创建时间戳目录：

```text
results/<YYYYMMDD_HHMMSS>/
├── functional/                # functional 或 all 模式
├── performance/               # performance 或 all 模式
└── restart/                   # restart 模式
```

单独运行某种模式时，只会创建对应子目录。

### 功能测试产物

- `summary.md`：适合人工阅读的结果摘要。
- `result.json`：结构化结果。
- `launch_args.json`：实际 Launch 参数。
- `launch.log`：驱动 Launch 输出。
- `functional.log`：功能测试阶段日志。
- `topic.log`：Topic 检查详情。
- `service.log`：Service 检查详情。
- `artifacts/image/`：保存图像服务生成的文件。
- `artifacts/point_cloud/`：保存点云服务生成的文件。

### 性能测试产物

- `summary.md`、`result.json`
- `launch_args.json`、`launch.log`
- `performance.log`
- `fps.csv`
- `system_usage.csv`
- `frame_timestamps/`：启用帧时间戳记录时生成。

排查失败时建议依次查看 `summary.md`、`result.json`、阶段日志和 `launch.log`。

## 项目结构

```text
auto_test_ws/
├── run_camera_auto_test.sh
├── run_camera_auto_test_ui.sh
├── src/
│   ├── orbbec_camera_auto_test/
│   │   ├── orbbec_camera_auto_test/
│   │   │   ├── checks/         # Topic、Service 检查
│   │   │   ├── core/           # ROS 会话、启动和报告基础能力
│   │   │   ├── profile/        # Profile 加载、继承和模板展开
│   │   │   └── runners/        # 各测试模式入口
│   │   ├── profiles/           # 基础和相机专用 YAML Profile
│   │   └── test/               # 自动化测试
│   └── orbbec_camera_auto_test_ui/
│       └── orbbec_camera_auto_test_ui/
│           ├── templates/
│           └── static/
└── results/                    # 运行时生成，不提交
```

## 可选构建方式

项目推荐直接通过两个根目录脚本运行。如果希望作为 ROS2 Python 包安装：

```bash
colcon build --packages-select orbbec_camera_auto_test
source install/setup.bash
```

安装后可使用：

```bash
run_functional_test --help
run_performance_test --help
run_restart_test --help
run_stream_stall_test --help
```

## 故障排查

### 找不到 ROS 环境

```bash
ls /opt/ros/humble/setup.bash
```

如果使用其他发行版，通过 `--ros-setup` 指定实际路径。

### 测试开始前提示未发现相机

先使用驱动自带的设备枚举或 Launch 命令确认：

- USB 设备已连接并有访问权限。
- 驱动环境与当前 ROS 版本一致。
- 没有其他进程独占相机。
- 序列号或 USB 端口参数正确。

### Topic 显示 SKIP

例如：

```text
[TOPIC][SKIP] /camera/depth_filter_status: topic not advertised
```

表示该接口存在于统一清单中，但当前 Launch 配置没有发布它，因此预检阶段将其排除。这通常不是失败；如果本次场景本应开启该接口，应检查 Profile 和实际 Launch 参数。

## 当前限制

- 功能与性能测试依赖真实相机在线。
- 当前内置 Profile 主要覆盖 Gemini 301 和 Gemini 330。
- 性能数据默认只生成报告，不设置跨机型统一硬阈值。
- `stream_stall` 尚未加入 `run_camera_auto_test.sh` 的 `--mode` 选项。
