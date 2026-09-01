# 开关流压测

English: [README.md](README.md)

## 工具介绍

启动指定 ROS launch，循环执行关流、开流、出流验证和存图。

| 功能 | 支持情况 |
| --- | --- |
| ROS 版本 | ROS 1、ROS 2 |
| 相机数量 | 单相机、预配置的多相机 launch |
| 开关方式 | 逐路开关；ROS 2 还支持整体开关 |
| 目标流 | 自动发现，或通过 `--image-topic` 指定 |
| 流配置切换 | A/B 两组分辨率、帧率和出流格式交替切换 |
| 开关时间 | 关流和开流+预览时间可独立配置，默认均为 4 秒 |
| 存图 | raw 彩色/IR 保存无损 PNG，raw 深度仅保存彩色渲染 PNG；`CompressedImage.data` 原样保存为 JPG |

逐路模式的典型流程：

```text
关闭一路流 → 确认该流无图像且其他流稳定
重新开启 → 收到新帧立即保存该路图像 → 确认所有流持续稳定
切换到下一路 → 完成所有目标流后进入下一循环
```

整体模式会关闭全部目标流，确认停流后整体恢复；收到新帧时逐路存图，再验证所有流持续稳定。
服务首次调用失败会重试一次；重试成功继续测试并记录警告，再次失败则标记本轮失败并尝试恢复流。
验证超时且某个话题一帧都未收到时，工具会重建该订阅及专用 executor，记录警告并重新
验证一次，并明确记录恢复成功或本轮仍失败。启用 `--continue-on-failure` 只会继续后续轮次，
不会把失败轮次或最终失败状态改成通过。ROS 2 只使用一个 rclpy context，以同时兼容 Fast DDS
和 Cyclone DDS；服务客户端在压测循环中保持复用，不再每轮创建和销毁。
点云和 IMU 订阅也在循环开始前建立并保持到测试结束；每次开流验证只重置采集窗口和
证据保存计数，不再反复销毁、重建 DDS reader。

ROS 1 仅支持默认的 `individual` 逐路模式；`all` 整体模式仅支持 ROS 2。

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
  --run-count 10
```

`--camera` 最多传入一次。未传入时，launch 使用自身默认相机参数。

### 多相机

多相机使用已配置好相机参数的 launch，不传 `--camera`：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file /path/to/multi_camera.launch.py \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_01/depth/image_raw \
  --image-topic /camera_02/color/image_raw \
  --image-topic /camera_02/depth/image_raw \
  --duration 1h
```

不传 `--image-topic` 时，工具自动选择存在对应 `toggle_<stream>` 服务的原始图像流。

### 可选功能

向 ROS launch 追加参数时可重复传入 `--launch-arg`，支持 `KEY=VALUE` 和
`KEY:=VALUE` 两种写法：

```bash
--launch-arg enable_point_cloud=false \
--launch-arg enable_colored_point_cloud=false
```

如果参数名称与脚本默认参数或 `--camera` 生成的参数相同，`--launch-arg` 的值优先。

ROS 2 整体开关：

```bash
--toggle-mode all
```

切换两组分辨率、帧率和出流格式：

```bash
--switch-stream-profile 1 \
--stream-profile-a /camera/color/image_raw=1280x720@30:MJPG \
--stream-profile-a /camera/depth/image_raw=640x480@30:Y16 \
--stream-profile-b /camera/color/image_raw=640x480@15:RGB888 \
--stream-profile-b /camera/depth/image_raw=320x240@15:Y16
```

A、B 两组必须配置相同的话题，且配置内容必须不同。

同时保存 raw PNG 和压缩图原始数据：

```bash
--image-topic /camera/color/image_raw \
--save-image-topic /camera/color/image_raw \
--save-image-topic /camera/color/image_raw/compressed
```

压缩话题必须是目标 `image_raw` 追加 `/compressed`。工具不解码或检查压缩数据，直接将
`CompressedImage.data` 保存为 `.jpg`。

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_DRIVER_SETUP` 或空 | 驱动环境 setup 脚本路径 |
| `--launch-file` | 必填 | launch 文件名或路径 |
| `--launch-arg` | 空 | 额外 launch 参数，格式为 `KEY=VALUE` 或 `KEY:=VALUE`；可重复传入，同名参数覆盖默认值 |
| `--camera` | 空 | 单相机 launch 参数，最多一个 |
| `--image-topic` | 自动发现 | 需要测试的原始图像流，可重复传入 |
| `--save-image-topic` | 目标 raw 话题 | 存图来源，可重复传入 |
| `--point-cloud-topic` | 循环前自动发现 | 每次开流后必检的 `PointCloud2` topic，可重复传入并支持 `{camera}` |
| `--imu-topic` | 循环前自动发现 | 每次开流后必检的 `Imu` topic，可重复传入并支持 `{camera}` |
| `--toggle-mode` | `individual` | `individual` 逐路；`all` 整体开关 |
| `--switch-stream-profile` | `0` | 设为 `1` 时在 A/B 流配置间切换 |
| `--stream-off-seconds` | `4` | 关流保持和验证时间，单位秒 |
| `--stream-on-preview-seconds` | `4` | 开流后的预览和验证时间，单位秒 |
| `--save-image-count` | `1` | 每个 topic 的产物数；恢复出流后立即开始图像采集，不等待稳定验证完成；`0` 仅检测 |
| `--skip-image-frames` | `0` | 每次恢复出流后对每个图像、点云和 IMU topic 分别跳过前 N 条消息，再开始采集 |
| `--run-count` | 空 | 最大完整循环数 |
| `--continue-on-failure` | 关闭 | 失败后恢复流并继续后续循环；最终结果仍为失败 |
| `--duration` | 空 | 最长运行时间，支持 `15m`、`2h` |

`--run-count` 和 `--duration` 至少传入一个，也可以同时传入；同时传入时，任一条件先达到即结束。

点云和 IMU 在循环开始前发现并固定为基线，每次重新开流后都必须恢复。
关流阶段不统一断言它们是否静默：点云通常依赖深度流，IMU 可能独立运行。
点云生成 XY/XZ/YZ 三视图，IMU 根据 accel、gyro 或同步话题生成曲线。

## 结果文件

每次运行会创建结果目录：

```text
stream_toggle_stress_test/results/YYYYMMDD_HHMMSS_stream_toggle_v2.0.0/
├── logs/camera.launch.log     # ROS launch 日志
├── logs/sdk/                  # SDK 日志
├── images/                    # raw 彩色/IR、彩色深度、点云和 IMU 证据
├── summary.md                 # 测试摘要
├── events.jsonl               # 结构化运行事件
└── result.json                # 每轮、每路流及存图结果
```

`summary.md` 会分别统计执行、通过、失败轮次和订阅恢复成功、失败次数，并列出失败轮次；
`result.json` 保留对应轮次的完整验证快照和恢复结果。
