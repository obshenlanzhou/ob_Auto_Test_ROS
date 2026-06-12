# OrbbecSDK_ROS1_v2 图像数据订阅端监测工具使用说明

## 1. 测试背景与结论

客户反馈在长时间运行过程中，ROS 图像流可能出现短暂停流、卡顿。为了验证当前版本在持续运行场景下的稳定性，可以使用 ROS 图像话题接收统计脚本，对指定的 Color、Depth 图像话题进行长时间订阅监测，并记录每一帧的 ROS header 时间戳和订阅端主机接收时间。

该脚本通过持续订阅 ROS 图像话题，并为每个话题累计接收时间差和 ROS header 时间戳时间差的最小值、最大值、平均值及异常次数，可以判断长时间运行过程中是否存在短暂停流、帧间隔异常、接收卡顿等问题。

压测结论：

* 使用该脚本进行长时间运行测试，未复现图像流停流问题。
* 如果客户现场仍存在短暂停流，建议优先切换到新版本进行验证。

## 2. 测试工具说明

[请至钉钉文档查看附件《image_topic_receive_stats.py》。](https://alidocs.dingtalk.com/i/nodes/KGZLxjv9VG3RB2QQC1pdMNpMV6EDybno?doc_type=wiki_doc&iframeQuery=anchorId%3DX02mpza60qt8isdoboka9g&utm_scene=team_space)

当前项目中的脚本路径：

```text
/home/yalian/work/camera/ob_Auto_Test_ROS/auto_test_ws/src/orbbec_camera_auto_test/scripts/image_topic_receive_stats.py
```

### 2.1 脚本用途

测试脚本：`image_topic_receive_stats.py`

该脚本用于订阅一个或多个显式指定的 `sensor_msgs/Image` 话题，并在订阅端累计接收统计数据。逐帧 CSV 可以按需关闭，但累计汇总文件 `summary.csv` 始终生成。它可以用于判断 ROS 层是否存在图像流短暂停止、帧间隔异常或接收延迟突增等问题。

脚本主要记录以下时间：

1. `ros_header_stamp_sec`：图像消息的 `header.stamp`，单位为秒，保留 6 位小数。对于 OrbbecSDK_ROS1_v2 发布端，该值来自发布端 `time_domain` 选择后的帧时间戳，默认通常为 global 时间。
2. `receive_system_ts_sec`：订阅回调开始处理该帧时的主机 Unix system clock 时间，单位为秒，保留 6 位小数。该字段适合与系统日志、驱动日志做绝对时间对齐。
3. `receive_steady_ts_sec`：订阅回调开始处理该帧时的主机 steady clock 时间，单位为秒，保留 6 位小数。该字段适合计算接收侧是否出现停流或卡顿。

时间精度说明：

* OrbbecSDK_ROS1_v2 发布端的帧时间戳来源为微秒级，例如 `timeStampUs()`、`globalTimeStampUs()`、`systemTimeStampUs()`。
* ROS `header.stamp` 使用 `sec + nsec` 表示，但发布端是将微秒乘以 1000 写入 ROS 时间戳，因此有效精度仍按微秒理解。
* 订阅端 CSV 中，绝对时间字段统一输出为 `_sec`，保留 6 位小数；时间差字段统一输出为整数微秒 `_us`。

### 2.2 指定监测话题

脚本没有默认监测话题。启动时必须显式指定一个或多个图像话题，否则脚本会立即报错退出。

命令行使用逗号分隔多个话题：

```bash
python3 image_topic_receive_stats.py \
  --topics "/camera_01/color/image_raw,/camera_01/depth/image_raw"
```

也可以通过 ROS 私有参数传入：

```bash
rosrun <your_package> image_topic_receive_stats.py \
  _topics:="[/camera_01/color/image_raw, /camera_01/depth/image_raw]"
```

话题配置规则：

* 支持绝对、相对和私有 ROS 话题名称。
* 脚本会将配置的话题解析为完整 ROS 名称，用于订阅、日志和 CSV 记录。
* 如果解析后存在重复话题，脚本会立即报错退出。
* 启动时话题尚未发布不会导致脚本退出，脚本会继续等待发布端。
* 指定的话题必须使用 `sensor_msgs/Image` 消息类型。

## 3. 测试方法

### 3.1 启动相机节点

先正常启动相机 ROS 节点，例如：

```bash
roslaunch orbbec_camera multi_camera.launch
```

也可以根据现场环境使用对应的 launch 文件。

启动后确认需要监测的图像话题：

```bash
rostopic list | grep image_raw
```

建议同时确认话题频率：

```bash
rostopic hz /camera_01/color/image_raw
rostopic hz /camera_01/depth/image_raw
```

### 3.2 使用 Python 启动接收统计脚本

先加载 ROS1 环境：

```bash
source /opt/ros/one/setup.zsh
```

示例：监测两个相机的 Color 和 Depth 图像话题，并输出 CSV 和 warning 日志：

```bash
python3 "/path/to/image_topic_receive_stats.py" \
  --topics "/camera_01/color/image_raw,/camera_01/depth/image_raw,/camera_02/color/image_raw,/camera_02/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --warning_interval_sec 1.0 \
  --warmup_sec 2.0 \
  --queue_size 10 \
  --buff_size 16
```

当前项目路径示例：

```bash
python3 "/home/yalian/work/camera/ob_Auto_Test_ROS/auto_test_ws/src/orbbec_camera_auto_test/scripts/image_topic_receive_stats.py" \
  --topics "/camera_01/color/image_raw,/camera_01/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --warning_interval_sec 1.0 \
  --warmup_sec 2.0
```

参数说明：

| 参数 | 说明 | 建议值 |
| --- | --- | --- |
| `--topics` | 必填，逗号分隔的 `sensor_msgs/Image` 话题列表 | 按实际监测话题设置 |
| `--output_dir` | CSV 和 warning 日志输出目录 | 每次测试使用独立目录 |
| `--warning_interval_sec` | 相邻两帧接收 steady clock 时间差超过该值时记录 warning | `1.0` 秒 |
| `--warmup_sec` | 启动预热时间，预热期间不记录逐帧数据、warning 或汇总时间差 | `2.0` 秒 |
| `--queue_size` | ROS Subscriber 队列大小 | `10` |
| `--buff_size` | ROS Subscriber socket buffer 大小，单位为 MB。高分辨率 raw image 或多路图像监测时可适当调大 | `16` |
| `--save_csv` | 是否保存逐帧 CSV，可设置为 `true` 或 `false` | `true` |
| `--disable_csv` | 不保存逐帧 CSV，仍保留 `summary.csv` 和 warning 日志 | 按测试需求使用 |

如果只想监测是否停流，不想保存大量逐帧 CSV，可以关闭逐帧 CSV。`summary.csv` 始终开启，不受该参数影响：

```bash
python3 "/path/to/image_topic_receive_stats.py" \
  --topics "/camera_01/color/image_raw,/camera_01/depth/image_raw" \
  --output_dir "./image_receive_stats_test" \
  --disable_csv
```

## 4. 输出数据说明

### 4.1 输出文件

脚本会在 `output_dir` 下生成：

```text
warnings.log
summary.csv
camera_01_color_image_raw.csv
camera_01_depth_image_raw.csv
```

启用逐帧 CSV 时，每个话题对应一个逐帧 CSV 文件，方便单独分析某一路图像是否异常。文件名根据解析后的完整话题名称生成。使用 `--disable_csv` 时不会生成各话题的逐帧 CSV 文件，长时间运行可选择关闭逐帧 CSV，只看 `summary.csv` 和 `warnings.log`。

`warnings.log` 和 `summary.csv` 始终生成。`summary.csv` 每 10 秒原子覆盖更新一次，并在节点正常退出时最终刷新；文件始终保持每个话题一行，不保存历史快照。

### 4.2 逐帧 CSV 字段说明

| 字段 | 含义 | 排查用途 |
| --- | --- | --- |
| `frame_index` | 脚本记录的订阅端帧序号 | 统计收到的帧数 |
| `topic` | 解析后的完整图像话题名称 | 区分不同相机或不同流 |
| `header_seq` | ROS 消息 `header.seq` | 判断消息序号是否连续 |
| `ros_header_stamp_sec` | ROS 消息 `header.stamp`，单位为秒，保留 6 位小数 | 判断发布端写入消息的时间戳是否连续 |
| `ros_header_stamp_delta_us` | 相邻两帧 ROS header 时间戳差，单位为微秒 | 判断消息源头是否停顿、回退或跳变 |
| `receive_system_ts_sec` | 回调收到帧时的主机 Unix system clock 时间，单位为秒，保留 6 位小数 | 与系统日志、驱动日志进行绝对时间对齐 |
| `receive_steady_ts_sec` | 回调收到帧时的主机 steady clock 时间，单位为秒，保留 6 位小数 | 记录接收侧时间点，不受系统墙上时间校准影响 |
| `receive_steady_delta_us` | 相邻两帧接收 steady clock 时间差，单位为微秒 | 判断 ROS 接收侧是否短暂停流或卡顿 |

system clock 可能受到手动校时或 NTP 校时影响，因此停流告警使用 `receive_steady_delta_us`，不使用 `receive_system_ts_sec` 计算时间差。

预热期间收到的帧不写入逐帧 CSV、不告警，也不参与汇总。预热结束后的第一帧会写入逐帧 CSV，但两个 delta 字段留空；从第二帧开始计算 delta，避免跨越预热边界的时间差污染统计。

### 4.3 summary.csv 字段说明

`summary.csv` 中每个话题占一行，统计值表示从预热结束后到最近一次更新时间的累计结果。

| 字段 | 含义 |
| --- | --- |
| `topic` | 解析后的完整图像话题名称 |
| `delta_count` | 实际参与汇总的相邻帧时间差数量 |
| `receive_steady_delta_min_us` | 最小接收 steady clock 时间差，单位为微秒 |
| `receive_steady_delta_max_us` | 最大接收 steady clock 时间差，单位为微秒 |
| `receive_steady_delta_average_us` | 平均接收 steady clock 时间差，单位为微秒 |
| `receive_steady_delta_warning_count` | 相邻两帧接收 steady clock 时间差超过 warning 阈值的累计次数 |
| `no_frame_warning_count` | 持续无新帧超过 warning 阈值的累计次数 |
| `max_receive_steady_delta_header_seq` | 最大接收时间差对应的当前帧 `header.seq` |
| `max_receive_steady_delta_system_ts_sec` | 最大接收时间差对应当前帧的 Unix system clock 时间，单位为秒，保留 6 位小数 |
| `ros_header_stamp_delta_min_us` | 最小 ROS header 时间戳差，单位为微秒，保留负值和零值 |
| `ros_header_stamp_delta_max_us` | 最大 ROS header 时间戳差，单位为微秒 |
| `ros_header_stamp_delta_average_us` | ROS header 时间戳正向时间差平均值，单位为微秒，排除零和负值 |
| `ros_header_stamp_non_positive_delta_count` | ROS header 时间戳差小于或等于零的累计次数 |
| `header_seq_gap_count` | `header.seq` 向前跳号的累计次数 |
| `header_seq_backward_count` | `header.seq` 回退的累计次数 |
| `header_seq_duplicate_count` | `header.seq` 重复的累计次数 |
| `image_info_change_count` | 图像 `width/height/encoding/step/data_size` 发生变化的累计次数 |
| `zero_data_count` | `len(data) == 0` 的空图像累计次数 |

最小值、最大值和平均值均按整数微秒输出。如果某个话题尚未产生可统计时间差，次数字段为 `0`，其余统计值和最大时间差定位信息留空。如果 ROS header 时间戳差全部小于或等于零，`ros_header_stamp_delta_average_us` 留空。

## 5. 判定方法

### 5.1 正常情况

以 30 FPS 图像流为例，理论帧间隔约为：

```text
1 / 30 = 0.0333 秒 = 33333 微秒
```

正常情况下，CSV 中的以下字段应大致在 `33333` 微秒附近波动：

```text
receive_steady_delta_us
ros_header_stamp_delta_us
```

如果是 15 FPS，理论帧间隔约为 `66667` 微秒。

### 5.2 短暂停流判断

如果 `receive_steady_delta_us` 明显超过正常帧间隔，例如达到 `500000`、`1000000` 或 `2000000` 微秒，说明 ROS 订阅端在这两帧之间没有收到该话题的新图像。

默认告警规则：

```text
receive_steady_delta_us > 1000000
或
距离上一帧的时间 > 1000000 微秒
```

满足该条件时，脚本会写入 `warnings.log`。

示例 warning：

```text
Image receive delta exceeded 1.000s: topic=/camera_01/color/image_raw, seq=12345, receive_steady_delta_us=1234567, ros_header_stamp_delta_us=1233333
```

该日志说明 `/camera_01/color/image_raw` 连续两帧之间的订阅端接收时间差超过 1 秒，存在短暂停流或严重卡顿。该类告警在下一帧到达时触发。

如果某一路图像持续停流，后续没有新帧到达，脚本会通过定时检查写入无帧告警：

```text
Image topic no frame for more than 1.000s: topic=/camera_01/color/image_raw, no_frame_duration_us=1234567, last_header_seq=12345, last_receive_system_ts_sec=1780541164.123456
```

该类告警不需要等待恢复出流。每次停流期间只记录一次；该话题恢复收到新帧后，下一次停流可以再次触发。

实时 warning 用于保留每次超阈值异常的上下文，不能被最终汇总完全替代。测试结束后可以先查看 `summary.csv`：

* `receive_steady_delta_max_us`：快速判断是否出现过较长接收时间差。
* `receive_steady_delta_warning_count`：判断恢复出流后发现的超阈值帧间时间差次数。
* `no_frame_warning_count`：判断测试期间是否出现过持续无新帧的停流。
* `max_receive_steady_delta_system_ts_sec`：定位最大接收时间差发生时间，并与其他系统日志对齐。
* `ros_header_stamp_non_positive_delta_count`：判断是否发生 ROS header 时间戳停滞或回退。
* `header_seq_gap_count`：判断是否发生消息序号跳号。
* `header_seq_backward_count`：判断是否发生消息序号回退。
* `header_seq_duplicate_count`：判断是否发生消息序号重复。
* `image_info_change_count`：判断图像分辨率、编码、步长或数据大小是否发生变化。
* `zero_data_count`：判断是否收到空图像数据。
* `delta_count`：判断汇总样本数量是否足够。

### 5.3 异常定位参考

| 现象 | 可能含义 |
| --- | --- |
| `receive_steady_delta_us` 和 `ros_header_stamp_delta_us` 同时增大 | 发布端、设备端或上游链路可能停顿 |
| 仅 `receive_steady_delta_us` 明显增大 | ROS 传输、订阅端调度或主机负载可能异常，需结合发布端监测工具分析 |
| `ros_header_stamp_delta_us` 为负数 | ROS header 时间戳发生回退 |
| `header_seq_gap_count` 增大 | 可能存在丢帧、发布端序号跳变或订阅端未收到部分消息 |
| `header_seq_backward_count` 增大 | 可能存在发布端重启、序号重置或异常回退 |
| `header_seq_duplicate_count` 增大 | 可能存在重复发布或 header 序号异常 |
| `image_info_change_count` 增大 | 图像分辨率、编码、步长或数据大小发生变化，需要确认是否符合预期 |
| `zero_data_count` 增大 | 收到空图像数据，需要检查发布端或链路异常 |

注意：该工具只能从订阅端判断一段时间内是否没有收到数据。如果确认存在停流，还需要结合发布端时间戳、驱动日志、系统负载和链路状态进一步分析。

## 6. 测试数据

### 数据来源

华峰

### 测试版本

ros wrapper 2.4.4（sdk 2.4.8）

### 运行配置

3x336+1x336L

[请至钉钉文档查看附件《test_launch.zip》。](https://alidocs.dingtalk.com/i/nodes/KGZLxjv9VG3RB2QQC1pdMNpMV6EDybno?doc_type=wiki_doc&iframeQuery=anchorId%3DX02mpyx4g9nhc81qfu730v&utm_scene=team_space)

### 测试结果

运行 14 小时，4 个相机时间戳间隔稳定，没有出现停流超 1 秒的现象。但出现了丢帧。

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/r4mlQ5b74D7VLlxo/img/3141bab8-c8c9-489d-b783-3b2120e033c1.png)

经分析，该帧的 global 时间戳出现回退，该问题在新版本中已修复，建议切换新版本测试。

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/r4mlQ5b74D7VLlxo/img/bae86dfa-3f7d-4a82-a945-dfc5137653ab.png)

[请至钉钉文档查看附件《custom-A-4x330-ros-244-14h-ok-260515.7z》。](https://alidocs.dingtalk.com/i/nodes/KGZLxjv9VG3RB2QQC1pdMNpMV6EDybno?doc_type=wiki_doc&iframeQuery=anchorId%3DX02mpytbdi8zntdfyd6lo&utm_scene=team_space)
