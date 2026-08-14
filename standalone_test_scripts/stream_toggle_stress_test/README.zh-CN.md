# 开关流压测

English: [README.md](README.md)

## 工具介绍

工具启动一个 ROS launch，对选定图像流循环执行关流、开流、验证和存图。支持的功能如下：

| 功能 | 支持情况 | 说明 |
| --- | --- | --- |
| ROS 版本 | ROS 1、ROS 2 | ROS 1 支持逐路开关；ROS 2 支持逐路和整体开关 |
| 相机数量 | 单相机、多相机 | 单相机参数可通过 `--camera` 传入；多相机使用预配置的多相机 launch |
| 目标流选择 | 自动发现、指定话题 | 可重复传入 `--image-topic`，留空时自动发现可开关的原始图像流 |
| 开关方式 | 逐路开关、整体开关 | 逐路调用 `toggle_<stream>`；整体调用每台相机的 `set_streams_enable` |
| 流配置切换 | 分辨率、帧率、出流格式 | 可配置 A、B 两组 profile，在完整循环开始时交替切换 |
| 开关时间 | 独立可配置 | 开流+预览时间和关流时间默认均为 4 秒，单位秒 |
| 出流验证 | 支持 | 验证停流、恢复稳定性、最大帧间隔、分辨率、实测帧率和 ROS encoding |
| 循环存图 | raw PNG、压缩原图 JPG | 每路验证完成后立即保存；每路可配置一个或多个存图话题 |
| 服务重试 | 支持 | 服务首次调用失败时重试一次；重试成功仍记录为警告 |
| 运行上限 | 时长、循环次数 | 可单独或同时配置，任一上限到达即结束 |

工具支持两种可配置的开关流压测模式：

- `--toggle-mode individual`（默认）：通过 `toggle_<stream>` 逐路关闭和恢复。
- `--toggle-mode all`：通过每台相机的 `set_streams_enable` 整体关闭和恢复全部流。

还可通过 `--switch-stream-profile 1` 启用分辨率、帧率和出流格式切换。工具接受 A、B
两组 `图像话题=宽x高@帧率:格式` 配置，按相机调用 `set_stream_profile`，在每个完整
循环开始时交替切换。切换后会验证全部目标流恢复、配置流的分辨率精确匹配、实测帧率
处于允许偏差内，且 ROS 图像 encoding 与指定 SDK 格式兼容；随后继续执行原有开关流
验证和存图，并再次确认配置没有在开关流后丢失。

格式字段可省略，此时驱动保持当前格式。格式名称不在工具中限制，由驱动根据相机实际
profile 校验，例如 `MJPG`、`RGB888`、`YUYV`、`Y8`、`Y16`。由于多个 SDK 格式可能
映射为同一个 ROS encoding（如彩色 `MJPG` 和 `RGB888` 均可为 `rgb8`），精确 SDK
格式以 `set_stream_profile` 成功选型为准，图像话题用于验证其 ROS 表示兼容性。
每台相机的 A/B 配置必须能从 `sensor_msgs/Image` 区分：分辨率或帧率不同即可；如果
只改变格式，则 ROS encoding 必须不同，例如 `BGR` 与 `RGB888`。相同分辨率/帧率下的
`MJPG` 与 `RGB888` 都映射为 `rgb8`，工具会在参数校验阶段拒绝这种无法观测的组合。

工具支持单相机和预配置的多相机 launch。开关模式的 ROS 版本支持情况如下：

| 开关模式 | ROS 1 | ROS 2 |
| --- | --- | --- |
| `individual` 逐路开关 | 支持 | 支持 |
| `all` 整体开关 | **不支持** | 支持 |

运行时切换分辨率/帧率使用独立的 `set_stream_profile` 服务，当前 ROS1 v2.9.3 和 ROS2
驱动均支持；它不会改变 ROS1 不支持 `all` 整体开关模式的限制。

当前 ROS1 v2.9.3 驱动只提供 `toggle_<stream>` 逐路服务，没有整体开关所需的
`set_streams_enable` 服务。因此 ROS1 必须使用默认的 `individual` 模式；若指定
`--toggle-mode all`，工具会在前置检查阶段明确失败，不会开始压测。ROS2 的 `all`
模式要求每台目标相机都提供 `/<camera_name>/set_streams_enable`。

逐路模式中，每路流的验证事务为：

```text
关闭目标流
  → 保持关流 4 秒：目标流持续无图像，同时全部其他目标流持续稳定
  → 重新开启目标流
  → 开流并连续预览、验证全部目标流 4 秒
  → 保存当前目标流图像
```

整体模式中，每个循环的验证事务为：

```text
依次调用所有目标相机的 set_streams_enable(false)
  → 保持关流 4 秒，全部目标图像流持续无图像
  → 依次调用 set_streams_enable(true)
  → 开流并连续预览、验证全部目标图像流 4 秒
  → 为每路目标流保存图像
```

多相机服务调用按相机命名空间确定性排序；每台相机的一次服务调用会整体开关该 launch
中为该相机启用的所有流。显式 `--image-topic` 控制验证清单，`--save-image-topic` 控制
存图清单，但整体服务仍会影响该相机所有已启用流。

驱动开关一路流时会重建相机 pipeline，因此工具会监控多相机 launch 中全部选中流，
检查同一相机及其他相机是否受到连带影响。逐路模式按相机命名空间、流名排序，一次只
操作一路；整体模式按相机命名空间排序，一次整体操作一台相机。

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
  --duration 1h
```

整体开关：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,usb-port=2-1 \
  --toggle-mode all \
  --run-count 10
```

逐路开关并在两组分辨率/帧率间切换：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,usb-port=2-1 \
  --switch-stream-profile 1 \
  --stream-profile-a /camera/color/image_raw=1280x720@30:MJPG \
  --stream-profile-a /camera/depth/image_raw=640x480@30:Y16 \
  --stream-profile-b /camera/color/image_raw=640x480@15:RGB888 \
  --stream-profile-b /camera/depth/image_raw=320x240@15:Y16 \
  --run-count 10
```

单相机配置中也可使用 `/{camera}/color/image_raw=...` 占位符；工具会使用
`--camera` 的 `name` 展开它。

ROS 1：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --camera name=camera,usb-port=2-1 \
  --run-count 10
```

ROS1 示例使用默认的 `--toggle-mode individual`；当前 ROS1 v2.9.3 驱动不支持
`--toggle-mode all`。

单相机可传入最多一个 `--camera`，字段格式与其他独立脚本相同：`name`、
`serial-number`、`usb-port`、`device-ip`、`device-port` 和 `config-file-path`。
这些字段会转换为 launch 参数。未传 `--camera` 时，不注入相机专属参数，launch 使用
自身默认值。

### 多相机 launch

多相机场景使用一份已配置好相机名称、SN/USB port、流开关、SDK 日志级别和日志文件名的 launch。
工具只启动一次该 launch，不要重复传入 `--camera`：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file /path/to/multi_camera.launch.py \
  --duration 1h
```

默认自动发现所有 `sensor_msgs/Image` topic，并只选择存在对应
`std_srvs/SetBool` 服务的流。例如：

```text
/camera_01/color/image_raw  → /camera_01/toggle_color
/camera_02/left_ir/image_raw → /camera_02/toggle_left_ir
```

`depth_to_color`、`confidence` 等没有对应 toggle 服务的派生 Image topic 会被跳过并写入
报告。也可以重复传入 `--image-topic`，严格指定必须出现和测试的流：

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --launch-file /path/to/multi_camera.launch.py \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_01/depth/image_raw \
  --image-topic /camera_02/color/image_raw \
  --image-topic /camera_02/depth/image_raw \
  --run-count 10
```

显式 topic 必须是标准的 `/<camera-namespace>/<stream>/image_raw`，类型必须为
`sensor_msgs/Image`，且对应 `toggle_<stream>` 服务必须存在，否则前置检查失败。
`{camera}` 占位符仅在传入单个 `--camera` 时可用。

未传 `--save-image-topic` 时，每路默认保存其目标 `image_raw`。需要同时保存原始图和
压缩图时，可为同一路重复指定 raw 话题及其严格对应的 `/compressed` 话题：

```bash
--image-topic /camera_01/color/image_raw \
--save-image-topic /camera_01/color/image_raw \
--save-image-topic /camera_01/color/image_raw/compressed
```

`sensor_msgs/Image` 以像素值无损的 PNG 保存（固定无损压缩级别 1），16 位深度值保持不变；
`sensor_msgs/CompressedImage` 不解码、不校验，直接将消息 `data` 原始字节保存为 `.jpg`。
压缩存图话题必须是已选目标 raw 话题追加 `/compressed`，且必须显式指定；自动发现只
选择原始图。`--save-image-count` 对每个存图话题分别生效。

多相机切换 profile 时，A、B 两组必须包含完全相同的话题集合；每台相机的一组配置会
在一次 `/<camera_name>/set_stream_profile` 请求中批量提交。例如：

```bash
  --switch-stream-profile 1 \
  --stream-profile-a /camera_01/color/image_raw=1280x720@30:MJPG \
  --stream-profile-a /camera_02/color/image_raw=640x480@30:RGB888 \
  --stream-profile-b /camera_01/color/image_raw=640x480@15:RGB888 \
  --stream-profile-b /camera_02/color/image_raw=320x240@15:MJPG
```

两组配置的话题必须属于最终选中的目标流；每台相机至少一路的分辨率、帧率或可观测
ROS encoding 必须不同。不在 A/B 配置中的其他目标流不会改变 profile，但仍参与全局
稳定性检查。

`--stream-off-seconds` 和 `--stream-on-preview-seconds` 分别控制关流保持时间、开流后的
预览验证时间，默认均为 4 秒，可独立配置任意正数，裸数字单位为秒。旧参数
`--stop-stable-seconds`、`--stable-seconds` 仍分别作为兼容别名。

## 循环、重试与停止

逐路模式的一个完整循环表示所有目标流各完成一次“关闭→验证→开启→验证→存图”；
整体模式的一个完整循环表示所有目标相机整体关闭、全部停流验证、整体开启、全部恢复
验证和逐路存图。同时设置
`--run-count` 和 `--duration` 时，任一上限先达到即停止。首个完整循环始终执行完，保证
每个目标流至少验证一次；后续到达时间上限时，会在当前单路事务完成恢复后停止。

服务调用默认超时 15 秒。首次调用失败时等待 1 秒重试一次：

- 重试成功：继续运行，最终仍可为 `passed`，但在 warnings 和步骤明细中记录。
- 两次失败：立即失败，尽力重新开启当前流，然后停止 launch。
- Ctrl+C/UI 停止：尽力恢复当前流并短暂确认出流，状态为 `interrupted`，退出码 130。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，`1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本 |
| `--driver-setup` | `$ORBBEC_DRIVER_SETUP` / `$ORBBEC_CAMERA_SETUP` 或空 | 驱动环境 setup 脚本 |
| `--launch-package` | `orbbec_camera` | launch 包名 |
| `--launch-file` | 必填 | launch 文件名或路径 |
| `--launch-arg` | — | 附加 launch 参数，可重复传入 |
| `--camera` | 空 | 单相机 launch 参数，最多一个 |
| `--image-topic` | 自动发现 | 严格指定目标原始图像流，可重复传入 |
| `--save-image-topic` | 目标 raw 话题 | 存图来源，可重复指定目标 raw 话题及其 `/compressed` 话题 |
| `--toggle-mode` | `individual` | `individual` 逐路开关（ROS1/ROS2）；`all` 整体开关（当前仅 ROS2） |
| `--switch-stream-profile` | `0` | `0` 不切换；`1` 在 A/B 两组分辨率、帧率和格式间交替切换 |
| `--stream-profile-a` | 空 | A 组配置，格式 `TOPIC=WIDTHxHEIGHT@FPS[:FORMAT]`，可重复传入 |
| `--stream-profile-b` | 空 | B 组配置，格式同上，话题集合必须与 A 组相同 |
| `--duration` | `300` | 最长运行时间，支持 `15m`、`2h` |
| `--run-count` | 空 | 最大完整循环数 |
| `--topic-discovery-timeout` | `30` | 话题/服务发现最长等待时间 |
| `--topic-discovery-settle` | `2` | 自动发现无新增目标的静默窗口 |
| `--stream-off-seconds` | `4` | 关流保持与连续验证时间，裸数字单位为秒；兼容别名 `--stop-stable-seconds` |
| `--stream-on-preview-seconds` | `4` | 开流后的预览与连续稳定验证时间；兼容别名 `--stable-seconds` |
| `--stream-timeout` | `20` | 每个关闭/开启状态验证超时 |
| `--max-gap-seconds` | `1.5` | 稳定窗口内最大帧接收间隔 |
| `--service-timeout` | `15` | 单次 toggle 服务调用超时 |
| `--service-retry-delay` | `1` | 首次服务失败后的重试等待时间 |
| `--profile-fps-tolerance` | `0.15` | profile 验证允许的实测帧率相对偏差，范围 0–1，最小绝对容差为 1 FPS |
| `--save-image-count` | `1` | 每个存图话题每循环保存数量，`0` 为关闭 |
| `--save-image-timeout` | `30` | 每路存图最长等待时间 |
| `--sdk-log-level` | `debug` | 单相机 launch 的 SDK 日志级别；多相机由 launch 预配置 |
| `--queue-size` | `10` | 图像订阅队列大小 |
| `--results-dir` | 自动生成 | 自定义结果目录 |

保存 raw PNG 需要 `cv_bridge` 和 OpenCV；只保存 `CompressedImage` 原始字节时不需要
图像解码。可通过 `--save-image-count 0` 禁用存图。

## 结果文件

```text
stream_toggle_stress_test/results/YYYYMMDD_HHMMSS_stream_toggle/
├── logs/
│   ├── camera.launch.log       # 整次运行的连续 launch 日志
│   └── sdk/                    # Orbbec SDK 日志目录
├── images/
│   ├── camera_01/color/image_0001.png
│   ├── camera_01/color/image_0002.jpg
│   └── camera_02/depth/image_0001.png
├── summary.md
├── events.jsonl
└── result.json
```

`result.json` 记录每个循环、每路服务调用及重试、停流和恢复耗时、帧统计、最大帧间隔、
恢复清理和图片路径。状态与退出码遵循统一独立脚本契约：`passed`/0、`failed`/1、
`interrupted`/130；命令行参数错误为 2。
