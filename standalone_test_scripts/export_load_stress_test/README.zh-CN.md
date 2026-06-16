# JSON 导入导出压测

English: [README.md](README.md)

## 用途

```text
按压测次数交替加载两份 JSON 参数文件，启动相机 launch，等待出流稳定后保存 JPG 图像、
导出 JSON，只对比 parameters 字段，验证导入参数是否生效。支持单相机和多相机。
```

## 基本用法

单相机：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts

python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera camera \
  --save-image-count 1 \
  --test-count 10
```

多相机：

```bash
python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file gemini_330_series_sdk_json.launch.py \
  --camera camera_01,usb_port=2-1 \
  --camera camera_02,usb_port=2-3 \
  --save-image-count 1 \
  --test-count 10
```

## JSON 配置

默认会交替使用：

```text
export_load_stress_test/config/Gemini_336L_1.json
export_load_stress_test/config/Gemini_336L_2.json
```

也可以重复传入 `--config-json` 指定 JSON 列表。

## 运行方式

脚本不会使用 `multi_camera.launch.py`。多相机时会为每台相机分别启动一个单相机
launch，并给每个 launch 传入：

```text
camera_name
load_config_json_file_path
usb_port 或 serial_number（如果配置了）
```

出流稳定后，脚本默认会为 color 和 depth image topic 各保存 1 张 JPG 图像。可以通过
`--save-image-count` 修改每轮每个 topic 的保存数量，设置为 `0` 表示不存图。

```bash
--save-image-count 3
```

默认监控和存图 topic：

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

可以重复传入 `--image-topic` 指定任意 image topic，例如 IR 或左右 IR：

```bash
--image-topic /{camera}/color/image_raw \
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/ir/image_raw
```

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw
```

随后脚本会调用：

```text
/{camera}/export_config_json
```

服务类型：

```text
orbbec_camera_msgs/srv/SetString
```

## 结果文件

每次运行会创建结果目录：

```text
export_load_stress_test/results/YYYYMMDD_HHMMSS_export_load/
```

文件说明：

```text
summary.md            # 最终结果和每次压测状态
result.json           # 完整机器可读结果
images/               # 每次压测保存的 JPG 图像
exports/              # 每次压测、每台相机导出的 JSON 和失败 diff
```
