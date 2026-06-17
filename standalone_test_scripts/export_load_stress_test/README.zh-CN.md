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
  --camera camera \
  --test-count 10
```

### 多相机

```bash
python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera camera_01,usb_port=2-1 \
  --camera camera_02,usb_port=2-3 \
  --test-count 10
```

`--camera` 格式：`name[,usb_port=PORT][,serial_number=SN]`

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--test-count` | `10` | 导入导出轮次数 |
| `--save-image-count` | `1` | 每轮每个 topic 保存的图片数（`0` = 不存图） |
| `--image-topic` | color + depth | 监控和存图的 topic，可重复传入 |
| `--config-json` | 见配置文件 | 交替使用的 JSON 文件，可重复传入 |

默认监控 topic：

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

重复传入 `--image-topic` 可增加其他流：

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
├── images/          # 每次压测/每台相机/每个 topic 的 JPG 图像
└── exports/         # 每次压测/每台相机的导出 JSON 和失败 diff
```
