# JSON 导入导出压测

English: [README.md](README.md)

## 工具介绍

交替加载两份 JSON 参数文件，启动相机 launch，等待出流稳定后保存图像，
通过 service 导出 JSON，只对比 `parameters` 字段，验证导入参数是否生效。

每次压测的典型流程：

```text
加载 JSON → 启动各相机 launch
等待所有流稳定 → 保存图像
通过 service 导出 JSON → 对比 parameters 字段
关闭 launch → 切换到下一份 JSON → 重复
```

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera name=camera \
  --run-count 10
```

### 多相机

```bash
python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera name=camera_01,usb-port=2-1 \
  --camera name=camera_02,usb-port=2-3 \
  --run-count 10
```

`--camera` 使用逗号分隔的 `KEY=VALUE` 格式。支持 `name`、`serial-number`、
`usb-port`、`device-ip`、`device-port`、`config-file-path`，每个字段均可选。

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` 或空 | Orbbec 驱动环境 setup 脚本路径 |
| `--launch-file` | `gemini_330_series_sdk_json.launch.py` | launch 文件名或路径 |
| `--camera` | 空 | 相机配置，可重复传入；格式见上文 |
| `--launch-arg` | — | 额外的 launch 参数（如 `enable_ir=true`），可重复传入，格式 `KEY=VALUE` 或 `KEY:=VALUE` |
| `--run-count` | `10` | 导入导出最大轮次数 |
| `--duration` | 空 | 可选的最长运行时间；任一已配置上限先达到即结束 |
| `--sdk-log-level` | `debug` | Orbbec SDK 日志级别 |
| `--save-image-count` | `1` | 每轮每个 topic 保存的图片数（`0` = 不存图） |
| `--image-topic` | 自动发现 | 指定后只监控并保存这些 topic，可重复传入 |
| `--config-json` | 见配置文件 | 交替使用的 JSON 文件，可重复传入 |

默认自动发现每个相机命名空间下所有已发布的 `sensor_msgs/Image` 图像流。
重复传入 `--image-topic` 后，将只监控并保存显式指定的流：

```bash
--image-topic /{camera}/color/image_raw \
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/ir/image_raw
```

### 配置文件

默认交替使用两份内置 JSON：

```text
export_load_stress_test/config/Gemini_336L_1.json
export_load_stress_test/config/Gemini_336L_2.json
```

替换为自定义文件时，重复传入 `--config-json`：

```bash
--config-json /path/to/config_A.json \
--config-json /path/to/config_B.json
```

## 结果文件

每次运行会创建结果目录：

```text
export_load_stress_test/results/YYYYMMDD_HHMMSS_export_load/
├── summary.md       # 最终结果和每次压测通过/失败状态
├── result.json      # 完整机器可读结果
├── events.jsonl     # 结构化生命周期和进度事件
├── images/          # 按相机及已启用图像流归类的 JPG 图像
│   ├── camera_01/color/image_0001.jpg
│   ├── camera_01/depth/image_0001.jpg
│   ├── camera_02/ir_left/image_0001.jpg
│   └── camera_02/ir_right/image_0001.jpg
├── exports/         # 每次压测/每台相机的导出 JSON 和失败 diff
├── logs/test_XXXX/<camera>/<camera>.launch.log  # 每轮 ROS launch 日志
└── logs/test_XXXX/<camera>/sdk/Log/<camera>/  # 每轮相机 SDK debug 日志
```

每台相机的每个流独立编号，并在后续压测轮次继续递增；已有图片不会被覆盖。
