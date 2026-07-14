# Orbbec Camera Auto Test

面向 Orbbec ROS 相机的通用自动化测试工作区，支持 ROS2、ROS1、命令行和本地 Web UI。单相机测试不再按相机型号维护 YAML：用户选择驱动 Launch，并按需指定开关流参数；测试框架只检查 ROS Graph 实际发现的接口。

## 测试能力

- 功能测试：检查已发现的 Topic、Service、数据内容、文件保存和设备重启恢复。
- 性能测试：统计已发现主数据流的 FPS、丢帧、CPU、内存和进程数。
- 丢帧分析：同时记录驱动端 CSV 与接收端帧时间戳。
- Launch 重启测试：重复启动驱动并验证图像流稳定恢复。
- 长时间断流测试：持续监控图像帧间隔并记录异常。
- 组合测试：依次执行功能测试和性能测试。

## 快速开始

进入工作区：

```bash
cd "$HOME/ORBBEC/ob_Auto_Test_ROS/auto_test_ws"
```

启动 Web UI：

```bash
./run_camera_auto_test_ui.sh
```

浏览器访问 `http://127.0.0.1:8000`。页面中选择 ROS 版本、单相机 Launch、测试模式和需要覆盖的流开关，然后填写驱动环境路径。

ROS2 命令行示例：

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --launch-file gemini_330_series.launch.py \
  --driver-setup /path/to/ros2_driver/install/setup.bash
```

ROS1 命令行示例：

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --launch-file gemini_330_series.launch \
  --ros-version 1 \
  --ros-setup /opt/ros/one/setup.bash \
  --driver-setup /path/to/ros1_driver/devel/setup.bash
```

查看参数：

```bash
./run_camera_auto_test.sh --help
```

## 环境

默认 ROS 环境：

- ROS2：`/opt/ros/humble/setup.bash`
- ROS1：`/opt/ros/one/setup.bash`

脚本先加载 ROS 环境，再加载 Orbbec 驱动环境。驱动环境使用 `--driver-setup` 或 `ORBBEC_DRIVER_SETUP` 传入，不在源码中写死。

Web UI 可分别预置两套驱动环境：

```bash
export ORBBEC_ROS2_CAMERA_SETUP=/path/to/ros2_driver/install/setup.bash
export ORBBEC_ROS1_CAMERA_SETUP=/path/to/ros1_driver/devel/setup.bash
./run_camera_auto_test_ui.sh
```

依赖包括 `PyYAML`、`psutil` 以及对应 ROS 版本的 `rclpy` 或 `rospy`。运行 `stress` 场景还需要安装 `stress-ng`。

## 通用单相机模型

单相机测试由三部分组成：

1. **Launch**：决定使用哪个驱动入口。
2. **流参数**：可选地覆盖 `enable_color`、`enable_depth`、`enable_ir`、左右 IR、点云、IMU 等参数；“默认”表示不传参数，由 Launch 决定。
3. **运行时发现**：只测试 ROS Graph 中实际发布的 Topic 和 Service。

因此新增相机通常只需要使用它已有的新 Launch，不需要创建相机专用测试 YAML。用户需要确认所选 Launch 是否支持显式传入的流参数。

Web UI 内置驱动包当前的单相机 Launch 候选：ROS2 19 个，ROS1 21 个。`lidar` Launch 不包含丢帧场景所需的驱动参数，因此不放入通用单相机列表。

### 特殊模式

两个模式需要驱动配置 YAML：

| Launch | UI 配置 | 传入文件 |
| --- | --- | --- |
| `gemini_301_series.launch.py` / `.launch` | Dual Color | `gemini305_dual_color.yaml`，并传入 `device_preset=Dual Color Streams` |
| `gemini2L.launch.py` / `.launch` | Dual IR | `gemini2L_dual_ir.yaml` |

特殊模式会传入 `config_file_path`，并禁用普通流开关，避免同一参数同时由 UI 和 YAML 控制。ROS2 由驱动 Launch 在包内解析配置文件；ROS1 运行时使用 `rospack find orbbec_camera` 定位当前 ROS1 驱动包自己的 `config/` 文件。

## 测试模式

| 模式 | 作用 |
| --- | --- |
| `functional` | 对统一目录中且已发现的 Topic、Service 执行功能检查 |
| `performance` | 对已发现的主数据流采集 FPS 与系统资源 |
| `restart` | 反复启动 Launch 并等待图像流稳定 |
| `stream_stall` | 长时间监控断流；当前由 Web UI 或独立 runner 使用 |
| `all` | 功能测试成功后继续执行性能测试 |

### 功能测试

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --launch-file gemini_301_series.launch.py \
  --launch-arg enable_color=true \
  --launch-arg enable_depth=true \
  --driver-setup /path/to/install/setup.bash
```

统一接口目录是 [all_topics_services.yaml](src/orbbec_camera_auto_test/profiles/base/all_topics_services.yaml)。执行流程为：

1. 检查相机是否连接。
2. 启动用户选择的 Launch。
3. 等待相机节点和基础 Service。
4. 获取 ROS Graph 快照。
5. 仅检查目录中且已发现的接口。
6. 输出 JSON、Markdown 和详细日志。

日志中的 `[TOPIC][SKIP] ... topic not advertised` 表示统一目录包含该接口，但当前 Launch 没有发布它；这是发现过滤结果，不是测试失败。

### 性能测试

通用性能场景固定为：

- `default`：监控所有已发现的主数据流。
- `stress`：在 `stress-ng` 负载下监控相同数据流。
- `drop_frame`：开启驱动与接收端时间戳记录，分析丢帧。

未传 `--scenario` 时依次运行全部场景：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --launch-file gemini_330_series.launch.py \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

只运行压力场景：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --launch-file gemini_330_series.launch.py \
  --scenario stress \
  --duration 15m \
  --driver-setup /path/to/install/setup.bash
```

性能候选包括 Color、Depth、IR、左右 IR、左右 Color、普通/彩色点云和 IMU。`camera_info`、metadata、压缩传输、TF、状态和 diagnostics 不参与性能采样。候选 Topic 仍需先通过 ROS Graph 发现。

`--duration` 支持秒数以及 `15m`、`2h`、`1d` 等格式，并覆盖场景默认时长。

### 丢帧分析

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --launch-file gemini_330_series.launch.py \
  --scenario drop_frame \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

该场景默认传入：

```text
enable_frame_drop_log=true
frame_timestamp_csv_file=<scenario_results_dir>/driver_frame_timestamp.csv
```

同时默认开启接收端帧时间戳记录。产物通常包括 `fps.csv`、`driver_frame_timestamp.csv` 和 `frame_timestamps/*.csv`。

### Launch 重启测试

```bash
./run_camera_auto_test.sh \
  --mode restart \
  --launch-file gemini_330_series.launch.py \
  --duration 30m \
  --image-topic /camera/color/image_raw \
  --stable-seconds 10 \
  --stream-timeout 60 \
  --max-gap-seconds 1.5 \
  --restart-delay 2 \
  --driver-setup /path/to/install/setup.bash
```

`--image-topic` 可以重复传入。

### 长时间断流测试

Web UI 可直接选择 `stream_stall`。命令行可调用独立 runner：

```bash
PYTHONPATH=src/orbbec_camera_auto_test \
python3 -m orbbec_camera_auto_test.runners.stream_stall \
  --launch-file gemini_330_series.launch.py \
  --results-dir results/stream_stall \
  --duration 30m \
  --image-topic /camera/color/image_raw \
  --driver-setup /path/to/install/setup.bash
```

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--mode` | `functional`、`performance`、`restart` 或 `all` |
| `--launch-file FILE` | 必填，选择驱动 Launch |
| `--launch-arg KEY=VALUE` | 覆盖 Launch 参数，可重复 |
| `--config-file-path PATH` | 使用自定义驱动配置文件 |
| `--scenario NAME` | `default`、`stress` 或 `drop_frame` |
| `--duration VALUE` | 覆盖性能或重启时长 |
| `--camera-name NAME` | 覆盖相机命名空间，默认 `camera` |
| `--serial-number SERIAL` | 指定序列号 |
| `--usb-port PORT` | 指定 USB 端口 |
| `--ros-version 1\|2` | 选择 ROS 版本 |
| `--ros-setup PATH` | ROS 环境脚本 |
| `--driver-setup PATH` | 驱动工作区环境脚本 |

Launch 参数优先级为：显式 `--launch-arg` 和专用参数，高于驱动 Launch 默认值。

## 结果目录

默认结果写入 `auto_test_ws/results/`；Web UI 写入 `results/ui_runs/`。常见文件：

- `result.json`、`summary.md`
- `launch_args.json`、`launch.log`
- `topic.log`、`service.log`
- `fps.csv`、`system_usage.csv`
- `driver_frame_timestamp.csv`、`frame_timestamps/*.csv`

`results/` 是运行产物，不应提交到 Git。

## 目录结构

```text
auto_test_ws/
├── run_camera_auto_test.sh
├── run_camera_auto_test_ui.sh
├── src/
│   ├── orbbec_camera_auto_test/
│   │   ├── orbbec_camera_auto_test/
│   │   │   ├── checks/
│   │   │   ├── core/
│   │   │   ├── profile/
│   │   │   └── runners/
│   │   └── profiles/
│   │       ├── base/all_topics_services.yaml
│   │       └── cameras/.../performance/   # 暂存的独立多相机配置
│   └── orbbec_camera_auto_test_ui/
└── results/
```

单相机功能和性能测试不再读取相机专用 Profile。多相机 Profile 暂时保留为独立能力，不出现在当前通用 Web UI 流程中。

## 验证

```bash
cd auto_test_ws
bash -n run_camera_auto_test.sh run_camera_auto_test_ui.sh
PYTHONPATH=src/orbbec_camera_auto_test:src/orbbec_camera_auto_test_ui \
python3 -m unittest discover -s src/orbbec_camera_auto_test/test -p 'test_*.py'
PYTHONPATH=src/orbbec_camera_auto_test:src/orbbec_camera_auto_test_ui \
python3 -m unittest discover -s src/orbbec_camera_auto_test_ui/test -p 'test_*.py'
```
