# `auto_test_ws` 自动化测试方案

## Summary
- 代码围绕两大模块实现：
  1. 功能测试
  2. 性能压测
- 两个模块独立，统一复用同一套“启动相机 + ROS 交互 + 日志归档”基础能力。
- 首版固定支持 `gemini_330_series.launch.py`，后续若扩展其他机型，只替换 launch 名和对应检查清单。

## Implementation Changes
### 1. 公共基础层
- 在 `auto_test_ws` 内新建一个独立测试包，例如 `orbbec_camera_auto_test`。
- 公共基础层只做 4 件事：
  - 环境准备
    负责 `source /opt/ros/humble/setup.bash`、`source Orbbec 驱动 install/setup.bash`
  - 启动与关闭相机
    统一封装 `ros2 launch orbbec_camera gemini_330_series.launch.py ...`
  - ROS 交互
    统一封装等待 node、topic、service、订阅消息、调用 service
  - 结果归档
    统一保存日志、JSON 结果、测试报告、图像和点云产物
- 公共层对外只暴露一个运行上下文 `TestSession`：
  - 启动 launch
  - 提供 `wait_for_topic` / `wait_for_service` / `call_service`
  - 结束时自动清理进程和保存日志

### 2. 功能测试模块
- 功能测试模块内部再分 3 个子流程，但都属于同一个模块：
  - 启动检查
  - 话题检查
  - 服务检查
- 入口脚本例如：
  - `run_functional_test.py`
- 执行流程固定：
  1. 创建 `TestSession`
  2. 启动 `gemini_330_series.launch.py`
  3. 等待相机节点和基础 service 就绪
  4. 执行话题测试
  5. 执行服务测试
  6. 汇总结果并输出报告
- 话题测试代码逻辑：
  - 准备一份“话题清单”
  - 对每个话题执行：
    - topic 是否存在
    - 是否在超时时间内收到消息
    - 消息是否有效
- 服务测试代码逻辑：
  - 准备一份“服务清单”
  - 分 3 类执行：
    - 只读服务
      例如 `get_sdk_version`、`get_device_info`
    - 状态切换服务
      例如曝光、增益、激光、LDP、流开关
    - 副产物服务
      例如 `save_images`、`save_point_cloud`
  - 状态切换类统一采用固定逻辑：
    - 读取当前状态
    - 设置测试值
    - 验证状态变化或可观测效果
    - 恢复原始状态
  - 副产物类统一采用固定逻辑：
    - 调 service
    - 检查输出目录是否生成新文件
  - `reboot_device` 放在功能测试最后：
    - 调用后等待相机重新上线
    - 再跑一轮最小启动检查和核心话题检查
- 功能测试输出：
  - `functional_result.json`
  - `functional_summary.md`
  - `launch.log`
  - `service.log`
  - `topic.log`

### 3. 性能压测模块
- 性能压测单独入口，例如：
  - `run_performance_test.py`
- 输入参数必须支持：
  - `duration`
  - `launch_file`
  - `camera_name`
  - 可选性能相关开关
- 执行流程固定：
  1. 创建 `TestSession`
  2. 启动 `gemini_330_series.launch.py`
  3. 等待核心话题稳定发布
  4. 启动性能采集器
  5. 持续运行指定时长
  6. 停止采集并生成报告
- 性能压测内部只做 2 类采集：
  - 图像流性能采集
    - 周期性统计 color/depth/IR/点云等 topic 的 FPS
    - 记录平均 FPS、最小 FPS、最大 FPS、采样点
  - 系统资源采集
    - 周期性统计相机相关进程 CPU、内存占用
    - 可选统计系统总 CPU、系统内存
- 首版实现方式：
  - FPS 采集用 ROS topic 订阅时间戳实现
  - 系统资源采集用 Python 定时读取进程信息
- 性能压测不和功能测试混写：
  - 不在压测过程中调用大量 service
  - 只保持相机稳定运行并持续采样
- 性能压测输出：
  - `performance_result.json`
  - `performance_summary.md`
  - `fps.csv`
  - `system_usage.csv`
  - `performance.log`

### 4. 统一运行入口
- 最外层提供一个总控脚本，例如：
  - `run_camera_auto_test.sh`
- 只负责调度，不负责具体测试逻辑。
- 支持 3 种模式：
  - `functional`
  - `performance`
  - `all`
- 执行规则固定：
  - `functional`：只跑功能测试
  - `performance`：只跑性能压测，必须传 `duration`
  - `all`：先跑功能测试，成功后再跑性能压测
- `all` 模式下若功能测试失败：
  - 性能压测不启动
  - 直接输出失败报告

## Public Interfaces / Files
- 建议代码文件收敛为以下几类：
  - `session.py`
    统一管理 launch 生命周期和日志
  - `ros_utils.py`
    统一管理 ROS topic/service/node 操作
  - `functional_runner.py`
    功能测试总流程
  - `functional_topics.py`
    话题检查逻辑
  - `functional_services.py`
    服务检查逻辑
  - `performance_runner.py`
    性能压测总流程
  - `performance_fps.py`
    FPS 采集逻辑
  - `performance_system.py`
    CPU/内存采集逻辑
  - `reporter.py`
    统一输出 JSON、Markdown、CSV
  - `run_camera_auto_test.sh`
    总入口脚本

## Test Plan
- 功能测试验收：
  - launch 能启动
  - 核心 topic 可收到合法消息
  - 核心 service 可调用
  - 保存图像/点云可生成文件
  - reboot 后相机能恢复
- 性能压测验收：
  - 可按指定时长稳定运行
  - 可输出 FPS 和系统资源统计文件
  - 压测过程中无异常退出
- `all` 模式验收：
  - 功能测试通过后，性能压测可自动接续执行

## Assumptions And Defaults
- 首版只实现 `gemini_330_series.launch.py`。
- 功能测试和性能压测严格分开。
- 默认执行顺序是先功能测试，再性能压测。
- 性能压测时间由运行参数指定，不在代码中写死。
