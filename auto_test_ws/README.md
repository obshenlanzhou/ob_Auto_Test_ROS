# Orbbec Camera Auto Test

面向 Orbbec ROS 相机的通用自动化测试工作区，支持 ROS2、ROS1、命令行和本地 Web UI。用户选择驱动 Launch，并按需指定开关流参数；功能测试根据 ROS 版本、Launch 和有效启动参数解析必选接口，同时继续检查 ROS Graph 中发现的可选接口。

## 测试能力

- 功能测试：校验必选 Topic、Service 是否齐全，并检查数据内容、文件保存和设备重启恢复。
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

浏览器访问 `http://127.0.0.1:8000`。顶部提供两个入口：

- **自动化框架**：选择 ROS 版本、单相机 Launch、测试模式和流开关。
- **独立脚本**：通过脚本旁的 `ui_manifest.json` 生成结构化表单，运行
  `standalone_test_scripts/` 中的六个工具。直接入口为
  `http://127.0.0.1:8000/?workspace=standalone`。

两个入口共用运行监控和历史归档，任一时刻只运行一个任务。固件与 Preset 升级会在
启动前二次确认，停止时等待当前升级操作到达安全点。

关闭 Web UI 时，`Ctrl+C` 或 `SIGTERM` 会先停止当前测试并等待结果状态落盘，再关闭
HTTP 服务。普通测试超时未退出时会依次发送 `SIGTERM`、`SIGKILL` 清理进程组；固件与
Preset 升级等 safe-point 任务会持续等待当前操作到达安全点，再次按 `Ctrl+C` 才强制终止。

顶部的“相机信息”会使用当前页面填写的 ROS 2 与 Camera ROS Setup 执行
`ros2 run orbbec_camera list_devices_node`，展示所有已连接相机的型号、PID、序列号、
连接方式、固件、USB 端口和 Preset。也可以直接调用同一接口：

```bash
curl -X POST http://127.0.0.1:8000/api/devices \
  -H 'Content-Type: application/json' \
  -d '{"ros_version":"2","ros_domain_id":"0","ros_setup":"/opt/ros/humble/setup.bash","camera_setup":"/path/to/driver/install/setup.bash"}'
```

响应中的 `devices` 是结构化相机列表，`output` 保留节点原始输出；该查询接口仅支持
ROS 2。

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

ROS 2 的 Domain ID 可在自动化框架和独立脚本页面配置，允许范围为 `0-232`。
配置后，测试进程以及顶部“相机信息”查询都会使用对应的 `ROS_DOMAIN_ID`；留空表示
不设置，执行前会清除从 Web UI 服务进程继承的 `ROS_DOMAIN_ID`。ROS 1 运行时不注入该变量。

依赖包括 `PyYAML`、`psutil` 以及对应 ROS 版本的 `rclpy` 或 `rospy`。运行 `stress` 场景还需要安装 `stress-ng`。

## 通用单相机模型

单相机测试由三部分组成：

1. **Launch**：决定使用哪个驱动入口。
2. **流参数**：可选地覆盖 `enable_color`、`enable_depth`、`enable_ir`、左右 IR、点云、IMU 等参数；“默认”表示不传参数，由 Launch 决定。
3. **必选接口与运行时发现**：按 ROS 版本、Launch 和流参数解析必选接口；其余接口按 ROS Graph 发现结果测试。

新增单相机 Launch 时，需要在必选接口表中登记其默认参数和适用型号；未登记的 Launch 会在设备探测和启动前失败。用户还需要确认所选 Launch 是否支持显式传入的流参数。

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
| `functional` | 校验 Launch 必选接口，并对已发现的 Topic、Service 执行功能检查 |
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

统一接口目录是 [all_topics_services.yaml](src/orbbec_camera_auto_test/profiles/base/all_topics_services.yaml)，ROS 版本/Launch 必选接口表是 [functional_required_interfaces.yaml](src/orbbec_camera_auto_test/profiles/base/functional_required_interfaces.yaml)。执行流程为：

1. 按 ROS 版本和 Launch 匹配必选接口配置；未配置时直接失败。
2. 合并 Launch 默认值、命令行/UI 覆盖值、驱动配置 YAML 和场景参数，解析本次必选接口。
3. 检查相机是否连接并启动用户选择的 Launch。
4. 等待相机节点和基础 Service，获取 ROS Graph 快照。
5. 必选接口缺失时记录失败；目录中的其他接口仍按发现结果检查。
6. 输出 JSON、Markdown 和详细日志，其中 Markdown 包含必选接口、Topic、Service、产物和重启结果表。

非必选接口未发布时仍会被发现过滤，不计为失败；必选接口未发布时会在结果表中显示 `required topic/service not advertised`，并使场景及整次功能测试失败。

自定义 `config_file_path` 会参与必选接口解析：可直接传入当前进程可读取的 YAML 路径；只传文件名时，需要在对应 Launch Profile 的 `config_overrides` 中登记。内置的 `gemini2L_dual_ir.yaml` 和 `gemini305_dual_color.yaml` 已登记。

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

- `result.json`、`summary.md`、`events.jsonl`
- `launch_args.json`、`launch.log`
- `topic.log`、`service.log`
- `fps.csv`、`system_usage.csv`
- `driver_frame_timestamp.csv`、`frame_timestamps/*.csv`

`results/` 是运行产物，不应提交到 Git。

独立脚本的 `result.json` 缺失、格式错误或 `test_id` 不匹配时，Web UI 会将该任务
标记为失败。每个独立脚本的表单值按脚本分别保存在本机 UI 配置中。

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
│   │       ├── base/functional_required_interfaces.yaml
│   │       └── cameras/.../performance/   # 暂存的独立多相机配置
│   └── orbbec_camera_auto_test_ui/
└── results/
```

单相机功能测试读取统一接口目录和 Launch 必选接口表，性能测试仍使用统一运行时发现模型。多相机 Profile 暂时保留为独立能力，不出现在当前通用 Web UI 流程中。

## 验证

```bash
cd auto_test_ws
bash -n run_camera_auto_test.sh run_camera_auto_test_ui.sh
PYTHONPATH=src/orbbec_camera_auto_test:src/orbbec_camera_auto_test_ui \
python3 -m unittest discover -s src/orbbec_camera_auto_test/test -p 'test_*.py'
PYTHONPATH=src/orbbec_camera_auto_test:src/orbbec_camera_auto_test_ui \
python3 -m unittest discover -s src/orbbec_camera_auto_test_ui/test -p 'test_*.py'
```
