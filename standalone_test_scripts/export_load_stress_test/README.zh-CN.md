# JSON 导入导出压测

English: [README.md](README.md)

## 用途

```text
按压测次数交替加载两份 JSON 参数文件，启动相机 launch，等待出流稳定后导出 JSON，
只对比 parameters 字段，验证导入参数是否生效。支持单相机和多相机。
```

## 基本用法

单相机：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts

python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera \
  --test-count 10
```

多相机：

```bash
python3 ./export_load_stress_test/export_load_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera_01,usb_port=2-1 \
  --camera camera_02,usb_port=2-3 \
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

出流稳定后，脚本会调用：

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
exports/              # 每次压测、每台相机导出的 JSON 和失败 diff
```
