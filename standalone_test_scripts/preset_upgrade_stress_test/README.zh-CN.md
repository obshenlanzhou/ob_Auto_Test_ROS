# Preset 升级压测

English: [README.md](README.md)

## 工具介绍

交替升级两份 optional depth preset bin 文件，每次升级成功后启动相机 launch 并传入
对应 `device_preset`，验证图像流是否稳定。可选择保存图像。

每次升级的典型流程：

```text
升级 preset bin → 启动 launch 并传入对应 device_preset
等待日志出现 "Loaded device preset:"
订阅图像 topic → 验证流稳定 → 保存图像
关闭 launch → 切换到下一份 preset → 重复
```

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --test-count 10 \
  --save-images-count 1
```

ROS 1：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --test-count 10
```

### 多相机

多次传入 `--camera`，建议配置 `usb_port` 或 `serial_number` 以避免选错设备：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --camera camera_01,usb_port=2-1 \
  --camera camera_02,usb_port=2-3 \
  --test-count 10 \
  --save-images-count 1
```

`--camera` 格式：`name[,usb_port=PORT][,serial_number=SN]`

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--test-count` | — | 升级轮次数；`0` = 持续运行到 `--duration` 或 Ctrl-C |
| `--duration` | — | 时间限制（如 `1h`），配合 `--test-count 0` 使用 |
| `--save-images-count` | `0` | 每轮每个 topic 保存的图片数（`0` = 不存图） |
| `--jpg-quality` | `95` | 保存图片的 JPEG 压缩质量（1–100） |
| `--image-topic` | color + depth | 监控的 topic，可重复传入；`{camera}` 自动展开为各相机名 |
| `--launch-arg` | — | 额外 launch 参数（如 `enable_left_ir=true`），可重复传入 |

默认监控 topic：

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

监控非默认流时，同时传入 `--image-topic` 和对应的 `--launch-arg`：

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw \
--launch-arg enable_left_ir=true \
--launch-arg enable_right_ir=true
```

### 配置文件

脚本交替使用 preset A 和 preset B。默认映射：

```text
config/g336x_K_High_Confidence_0.0.2.bin → device_preset: K High Confidence
config/g336x_K_High_Accuracy_0.0.2.bin   → device_preset: K High Accuracy
```

替换为自定义文件时，显式传入路径和名称：

```bash
--preset-a-path /path/to/a.bin --preset-a-name "K Clean Medium Confidence" \
--preset-b-path /path/to/b.bin --preset-b-name "K High Accuracy"
```

## 结果文件

每次运行会创建结果目录：

```text
preset_upgrade_stress_test/results/YYYYMMDD_HHMMSS_preset_upgrade/
├── summary.md                              # 最终摘要
├── result.json                             # 完整机器可读结果
├── logs/test_XXXX/<camera>/upgrade.log     # firmware_update_tool 输出
├── logs/test_XXXX/<camera>/launch.log      # launch 输出
└── images/test_XXXX/<camera>/             # 保存的 JPG 图像（启用存图时）
```
