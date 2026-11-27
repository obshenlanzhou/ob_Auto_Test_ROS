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

通过 Git 克隆的工作区可以在本机 Web UI 顶部打开「在线更新」。点击「检查更新」查看
当前提交和远端最新提交，然后点击「更新并重启」。更新跟踪当前分支的上游，可使用
GitHub 或 Gitee。工作区需干净，测试和结果打包需结束，分支可快进时才会应用更新。

关闭 Web UI 时，`Ctrl+C` 或 `SIGTERM` 会先停止当前测试并等待结果状态落盘，再关闭
HTTP 服务。普通测试超时未退出时会依次发送 `SIGTERM`、`SIGKILL` 清理进程组；固件与
Preset 升级等 safe-point 任务会持续等待当前操作到达安全点，再次按 `Ctrl+C` 才强制终止。

顶部的“相机信息”会使用当前页面填写的 ROS 2 与 Camera ROS Setup 执行
`ros2 run orbbec_camera list_devices_node`，展示所有已连接相机的型号、PID、序列号、
连接方式、固件、USB 端口和 Preset。也可以直接调用同一接口：

```bash
curl -X POST http://127.0.0.1:8000/api/devices \
  -H 'Content-Type: application/json' \
  -d '{"ros_version":"2","ros_domain_id":"0","ros_localhost_only":true,"ros_setup":"/opt/ros/humble/setup.bash","camera_setup":"/path/to/driver/install/setup.bash"}'
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
“仅本机通信”开关默认开启，会限制上述 ROS 2 进程只通过本机接口发现和通信。加载 ROS 环境后，
Foxy、Galactic、Humble 及更早发行版使用 `ROS_LOCALHOST_ONLY=1`；Iron 及更新发行版使用
`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`。关闭时会显式清除这两个变量，避免继承 Web UI
服务进程的发现范围配置。该开关可与 Domain ID 组合使用，以同时隔离其他主机和本机上的
不同测试组。独立脚本在其内部加载 ROS 环境，因此启动包装层会同时导出两个兼容变量；
对应 ROS 版本会使用其支持的变量。ROS 1 不注入上述变量。

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
| `transport_latency` | Web UI 中的 ROS 2 传输延迟测试，支持同进程 IPC 或跨进程 |
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

节点和基础服务就绪后，功能 runner 会等待最多 10 秒并持续刷新 ROS Graph，兼容 `/tf_static` 等首帧到达后才发布的必选接口。等待超时仍缺失时，才停止当前 Launch 并跳过后续检查；等待期间出现 `[ERROR]`、`[FATAL]`（例如 `Failed to initialize device`）仍会立即终止。

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

### 单版本传输延迟测试

在 Web UI 的 **测试模式** 下拉框中直接选择 **传输延迟**，
然后选择通信方式和采样时长。每次运行使用当前选择的 Camera ROS Setup 和 Launch。
测试组件由自动化工作区独立构建，旧版驱动可以直接复用。

**接收方式** 默认选择标准 ROS。选择相机 Launch 或修改驱动环境后，UI 检查 GPU 后端并显示可用的对比路径。
选择 **GPU 对比** 后，采样时长按阶段填写，旁边显示两阶段的合计采样时间。预热设置位于 **高级设置**。
NITROS 对比 CPU DDS 与 NITROS IPC，CUDA Buffer 对比接收 CPU 数据后上传和直接访问共享 CUDA 缓冲。
当前驱动的两种 GPU 测试都从帧时间戳计时到订阅回调开始。`time_domain=device` 时，该时延显示为
`n/a`，FPS 继续统计。旧版 CUDA Buffer 驱动缺少端到端时延数据时，该时延也显示为 `n/a`。
GPU 示例使用固定的 QoS、通信方式和每阶段 60 秒的预热超时。采样时长按阶段配置，范围为 1–3600 秒。
表格按流排列两种模式，显示流、分辨率、格式、模式、接收后端、FPS、端到端平均和 P95 时延以及整机 CPU/GPU 占用。
原始 GPU 示例汇总保存在 `gpu_driver_summary.csv`，可以查看样本数、最长收帧间隔及 CUDA Buffer 的 CPU 回退帧。
命令行可用 `--transport-receiver gpu` 选择同一模式。

#### 指定多相机 Launch 和必测话题

传输延迟页面默认显示单相机配置。在 **相机配置** 点击 **多相机 → 配置相机**，在弹窗选择启动方式、
设置设备和必测流，然后保存。可逐台配置，也可复用已有的多相机 Launch。页面保存后直接使用这份配置。
命令行可通过 `--transport-camera-config` 传入 YAML。下面的配置复用已经设好设备和流开关的
`multi_camera.launch.py`。

```yaml
launch: multi_camera.launch.py
cameras:
  - name: camera_01
    streams: [color, depth]
  - name: camera_02
    streams: [color, depth]
```

Launch 可以使用绝对路径、相对 YAML 所在目录的路径，或已选驱动安装中的 Launch 文件名。流列表中的
名称可以是 `color`、`depth`、`ir`、`left_ir`、`right_ir`、`left_color`、`right_color`、`points`、
`colored_points`。话题经过重映射时，可以写成 `streams: {color: /rig/cam1/color/image_raw}`。
NITROS 的 GPU 话题有单独映射时，可用 `gpu_topics: {color: /rig/cam1/color/image_raw/nitros_image}` 指定。
指定的流都必须在本阶段完成预热，然后统一开始采样。报告按相机和流记录 FPS、端到端平均时延及 P95。
如果相机的帧时间戳用设备时钟，在对应相机下填写 `time_domain: device`。

对于标准 ROS 同进程 IPC 和 NITROS，Launch 需要声明并向相机转发
`component_container_name`、`use_intra_process_comms`。预配置的多相机 Launch 没有这些参数时，
可在 YAML 中为每台相机单独指定 Launch，使用序列号或 USB 端口定位设备。

```yaml
cameras:
  - name: camera_01
    launch: gemini_330_series.launch.py
    arguments: {serial_number: "实际序列号1", enable_color: true, enable_depth: true}
    streams: [color, depth]
  - name: camera_02
    launch: gemini_330_series.launch.py
    arguments: {serial_number: "实际序列号2", enable_color: true, enable_depth: true}
    streams: [color, depth]
```

```bash
./run_camera_auto_test.sh --mode performance --scenario transport_latency \
  --ros-setup /opt/ros/jazzy/setup.bash --driver-setup /path/to/driver/install/setup.bash \
  --transport-camera-config /path/to/transport_cameras.yaml --duration 10
```

用 `--transport-receiver gpu` 可运行多机 GPU 对比。测试工作区按所选驱动的 GPU 后端构建订阅组件。
NITROS 把订阅组件加载到相机所在容器，CUDA Buffer 保持跨进程，核验实际接收的缓冲后端。
使用预配置的多相机 Launch 时，子相机也需要开启 GPU 发布。
两种模式都统一切换 CPU/GPU 阶段，并单独记录读失败及 CUDA CPU 回退。结果文件为
`transport_summary.csv`、`result.json` 和 `summary.md`。

**流配置** 根据所选 ROS 2 Launch 显示可用开关。将一路图像设为「开启」后，在该流下方可填写宽度、高度、
FPS 和格式，支持彩色、深度、单红外、左右红外及左右彩色。留空采用 Launch 默认值，
相机 YAML 和额外 Launch 参数中的同名设置优先。Gemini 301 系列的「双彩色」和 Gemini 2L 的
「双红外」由预设启用左右两路，可分别覆盖其图像参数，留空采用预设值。

运行 `./run_camera_auto_test_ui.sh` 时会自动为当前可确定的 ROS 2 环境编译测试组件，
并按 ROS 2 安装路径和 CPU 架构复用增量构建。启动后如果切换 ROS 2 环境，首次运行传输延迟测试时
会自动准备该环境的组件。编译需要当前机安装 `colcon` 和对应的 ROS 2 开发依赖。

已有独立测试组件时，可在启动 UI 前通过 `ORBBEC_TRANSPORT_SETUP` 指定对应的 `local_setup.bash`。

| 通信方式 | 测量路径 |
| --- | --- |
| 跨进程 | 驱动和 C++ 订阅器位于独立进程，使用标准 ROS 图像和点云消息 |
| 同进程 IPC | 驱动和同一 C++ 订阅器位于同一 ROS 2 组件容器，只采集实际经 IPC 交付的标准消息 |

同进程需要可组合的相机节点。支持能加入外部容器的 Launch，以及可加载标准相机组件的旧式
Orbbec 单相机 Launch。两种方式都支持选择流，明确开启的流必须参与测试，其余由发布话题发现。
明确关闭的流会跳过。标准传输测试在 Launch 声明支持时默认设置 `time_domain=global`
和 `enable_gpu_transport=false`，可通过额外 Launch 参数覆盖。

**订阅可靠性（QoS）** 可选择 Best Effort（默认）或 Reliable，两种设置都使用 `KEEP_LAST(1)`。
选择 Reliable 时，相机发布端也需提供 Reliable。实时表格和报告会显示本轮的订阅设置。

在 **测试配置** 中可填写每路预热帧数和预热超时，留空分别使用 30 帧和 60 秒。
超时涵盖等待话题出帧和预热，所有参与话题达到帧数后才开始计时，默认采样 60 秒。
延迟从图像或点云
`header.stamp` 算到 C++ 订阅回调入口，使用主机系统时钟。报告提供样本数、接收 FPS、
平均延迟、P95、无效时间戳和传输方式不符的帧数。资源统计覆盖测试进程树。
UI 表格每秒按话题更新样本数、当前和平均 FPS、平均延迟，并在采样结束时显示 P95。
结果保存在 `transport_summary.csv`、`result.json` 和 `summary.md`。
结果会记录实际加载的驱动路径和可读取的 SDK 校验值。

命令行也可运行同一场景，将 `--transport-mode` 改为 `intra_process` 可测试同进程。

```bash
./run_camera_auto_test.sh --mode performance --scenario transport_latency \
  --transport-mode cross_process --duration 10m \
  --transport-reliability reliable \
  --transport-warmup-frames 30 --transport-warmup-timeout-sec 60 \
  --launch-file gemini_330_series.launch.py \
  --ros-setup /opt/ros/jazzy/setup.bash \
  --driver-setup /path/to/driver/install/setup.bash
```

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
