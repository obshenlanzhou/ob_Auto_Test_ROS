# Orbbec Camera Auto Test 使用文档

## 1. 说明

`auto_test_ws` 提供一套面向 Orbbec ROS2 相机的自动化测试工具，当前首版固定支持：

- `gemini_330_series.launch.py`

测试能力分成两个独立模块：

1. 功能测试
2. 性能压测

也支持按顺序执行：

1. 先跑功能测试
2. 功能通过后再跑性能压测

当前统一入口脚本为：

- [run_camera_auto_test.sh](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/run_camera_auto_test.sh)
- [run_camera_auto_test_ui.sh](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/run_camera_auto_test_ui.sh)


## 2. 当前目录结构

核心文件如下：

- [run_camera_auto_test.sh](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/run_camera_auto_test.sh)
- [run_camera_auto_test_ui.sh](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/run_camera_auto_test_ui.sh)
- [README.md](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/README.md)
- [gemini_330_series.yaml](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/src/orbbec_camera_auto_test/profiles/gemini_330_series.yaml)
- [functional_runner.py](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/src/orbbec_camera_auto_test/orbbec_camera_auto_test/functional_runner.py)
- [performance_runner.py](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/src/orbbec_camera_auto_test/orbbec_camera_auto_test/performance_runner.py)


## 3. 环境要求

运行前需要满足以下条件：

- 系统已安装 ROS2 Humble
- `orbbec_camera` 驱动已经可用
- 目标相机已经连接并能被驱动正常启动
- Python 依赖可用：
  - `rclpy`
  - `PyYAML`
  - `psutil`

脚本运行时会自动：

- `source /opt/ros/humble/setup.bash`
- 如提供 `--driver-setup`，则额外 `source` 对应驱动环境
- 清理旧的 `ROS_DISTRO` / `ROS_ETC_DIR`，避免混用其他 ROS 环境


## 4. 驱动环境准备

如果 Orbbec 驱动已经编译并安装到某个工作区，先确认对应 `setup.bash` 路径，例如：

```bash
/home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash
```

运行测试时可以通过以下两种方式提供：

1. 命令行显式传入：

```bash
./run_camera_auto_test.sh --mode functional --driver-setup /path/to/install/setup.bash
```

2. 通过环境变量提供：

```bash
export ORBBEC_DRIVER_SETUP=/path/to/install/setup.bash
./run_camera_auto_test.sh --mode functional
```


## 5. 快速开始

进入工作区目录：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test/auto_test_ws
```

### 5.1 只跑功能测试

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --driver-setup /path/to/install/setup.bash
```

### 5.2 只跑性能压测

例如压测 300 秒：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

### 5.3 顺序执行功能测试 + 性能压测

```bash
./run_camera_auto_test.sh \
  --mode all \
  --duration 300 \
  --driver-setup /path/to/install/setup.bash
```

说明：

- `all` 模式会先跑功能测试
- 只有功能测试通过后，才会继续跑性能压测


## 6. 常用参数

脚本支持的主要参数如下：

- `--mode functional|performance|all`
- `--duration SECONDS`
- `--profile PROFILE_NAME_OR_PATH`
- `--camera-name NAME`
- `--serial-number SERIAL`
- `--usb-port PORT`
- `--config-file-path PATH`
- `--driver-setup PATH`
- `--results-root PATH`
- `--launch-file FILE`
- `--launch-arg KEY=VALUE`

查看帮助：

```bash
./run_camera_auto_test.sh --help
```


## 7. Web UI

项目也提供一个本地 Web UI。这个 UI 不是 ROS2 package，不需要 `colcon build` 或 `ros2 run`。

启动方式：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test/auto_test_ws
./run_camera_auto_test_ui.sh
```

浏览器访问：

```text
http://127.0.0.1:8000
```

如需指定监听地址或端口：

```bash
./run_camera_auto_test_ui.sh --host 127.0.0.1 --port 8001
```

UI 启动测试时会作为 CLI proxy 执行测试命令，并自动 source：

```bash
source /opt/ros/humble/setup.bash
source <页面中填写的 Camera ROS setup.bash 或 setup.zsh>
```

`Camera ROS setup.bash` 默认会自动填入：

```text
/home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash
```

如果使用 `setup.zsh`，UI 后端会切换到 zsh 执行测试命令，并优先 source `/opt/ros/humble/setup.zsh`。UI 会把测试记录写入：

```text
results/ui_runs/
```

性能压测运行时，UI 会实时读取当前结果目录中的 `system_usage.csv` 和 `fps.csv`，展示已压测时间、CPU 占用、RAM 占用、进程数和各图像话题 FPS。

最近一次 UI 配置会保存到：

```text
results/ui_config.json
```


## 8. 常见用法示例

### 8.1 指定相机名

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --camera-name camera \
  --driver-setup /path/to/install/setup.bash
```

### 8.2 指定序列号

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --serial-number SN123456789 \
  --driver-setup /path/to/install/setup.bash
```

### 8.3 指定 USB 端口

```bash
./run_camera_auto_test.sh \
  --mode functional \
  --usb-port 2-7 \
  --driver-setup /path/to/install/setup.bash
```

### 8.4 指定自定义 launch 参数

例如覆盖分辨率：

```bash
./run_camera_auto_test.sh \
  --mode performance \
  --duration 300 \
  --launch-arg color_width=1280 \
  --launch-arg color_height=800 \
  --launch-arg color_fps=30 \
  --driver-setup /path/to/install/setup.bash
```

### 8.5 自定义结果输出目录

```bash
./run_camera_auto_test.sh \
  --mode all \
  --duration 600 \
  --results-root /tmp/orbbec_results \
  --driver-setup /path/to/install/setup.bash
```


## 9. 功能测试说明

功能测试现在按“启动场景”执行。

每个场景都对应：

- 一组 `launch_args`
- 一组要检查的 topic
- 一组要检查的 service

当前 `gemini_330_series.yaml` 里已经定义了这些场景：

- `default`
- `extrinsics_enabled`
- `ir_enabled`
- `imu_enabled`

### default 场景

默认启动 `gemini_330_series.launch.py`，验证：

- 你列出的默认话题是否出现或能收到消息
- 你列出的默认服务是否出现
- 一部分只读服务是否能正常调用
- `save_images` / `save_point_cloud` 是否能生成文件
- `/camera/reboot_device` 后是否能恢复

### 其他参数场景

其余场景会在默认参数基础上追加对应启动参数，然后测试该参数组合下的专属话题：

- `extrinsics_enabled`
  - `enable_publish_extrinsic=true`
- `ir_enabled`
  - `enable_left_ir=true`
  - `enable_right_ir=true`
- `imu_enabled`
  - `enable_accel=true`
  - `enable_gyro=true`


## 10. 性能压测说明

性能压测会独立启动一轮干净的 launch，不复用功能测试实例。

当前统计内容包括：

### 图像流性能

基于 YAML profile 中的 `performance_topics` 统计：

- 平均 FPS
- 最小 FPS
- 最大 FPS
- 消息数量

### 系统资源占用

基于 launch 进程树 PID 集合统计：

- CPU 占用
- RSS 内存占用
- 进程数

当前首版为“报告优先”：

- 若相机进程异常退出或采集失败，则判失败
- 性能数值先统计并输出，不做硬阈值拦截


## 11. 结果目录说明

每次运行都会在结果根目录下生成一个时间戳目录：

```text
results/<run_id>/
```

其中功能测试和性能压测分别独立归档：

```text
results/<run_id>/functional/
results/<run_id>/performance/
```

### 功能测试目录

典型产物包括：

- `launch.log`
- `launch_args.json`
- `result.json`
- `summary.md`
- `topic.log`
- `service.log`
- `artifacts/image/`
- `artifacts/point_cloud/`

### 性能压测目录

典型产物包括：

- `launch.log`
- `launch_args.json`
- `result.json`
- `summary.md`
- `performance.log`
- `fps.csv`
- `system_usage.csv`


## 12. YAML Profile 说明

当前默认 profile 文件：

- [gemini_330_series.yaml](/home/slz/ORBBEC/ob_Auto_Test/auto_test_ws/src/orbbec_camera_auto_test/profiles/gemini_330_series.yaml)

该文件负责描述：

- 使用哪个 launch 文件
- 默认 launch 参数
- 基础话题清单
- 基础服务清单
- 功能组
- 副产物服务
- 性能压测关注的话题

如果后续要支持其他机型，建议做法是：

1. 新增一个新的 profile YAML
2. 保持主测试逻辑不变
3. 通过 `--profile` 选择对应机型


## 13. 可选构建方式

当前推荐直接使用根目录脚本运行，不强制先构建。

如果希望按 ROS2 包方式安装，也可以在 `auto_test_ws` 下执行：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test/auto_test_ws
colcon build --packages-select orbbec_camera_auto_test
source install/setup.bash
```

然后也可以直接调用 Python 入口：

```bash
python3 -m orbbec_camera_auto_test.functional_runner --help
python3 -m orbbec_camera_auto_test.performance_runner --help
```


## 14. 当前限制

当前实现有以下限制：

- 首版只支持 `gemini_330_series.launch.py`
- 功能测试和性能压测都依赖真实相机在线
- 性能压测当前只做统计和报告，不做硬阈值判定
- YAML profile 目前只提供一个机型模板


## 15. 故障排查

### 15.1 找不到 ROS2 Humble

检查：

```bash
ls /opt/ros/humble/setup.bash
```

### 15.2 驱动环境无效

检查：

```bash
source /path/to/install/setup.bash
ros2 pkg list | grep orbbec_camera
```

### 15.3 相机起不来

建议先手动验证：

```bash
source /opt/ros/humble/setup.bash
source /path/to/install/setup.bash
ros2 launch orbbec_camera gemini_330_series.launch.py
```

### 15.4 没有生成图像或点云产物

检查：

- `save_images` / `save_point_cloud` 服务是否真的可用
- 测试日志中是否有 service 调用失败
- 当前工作目录是否有写权限


## 16. 建议运行顺序

建议首次联调时按以下顺序：

1. 手动启动一次相机 launch，确认驱动正常
2. 运行 `functional` 模式
3. 查看 `functional/summary.md`
4. 功能稳定后，再运行 `performance` 或 `all` 模式
