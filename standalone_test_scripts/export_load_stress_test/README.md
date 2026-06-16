# JSON Import/Export Stress Test

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Purpose

```text
Alternate between config JSON files for a fixed test count, start camera launch
per camera, wait for stable streams, save JPG images, export the current JSON
settings, and compare only the parameters field.
```

## Basic Usage

Single camera:

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

Multiple cameras:

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

## JSON Configs

By default, the script alternates between:

```text
export_load_stress_test/config/Gemini_336L_1.json
export_load_stress_test/config/Gemini_336L_2.json
```

Pass `--config-json` repeatedly to override the JSON cycle.

## Runtime Model

The script does not use `multi_camera.launch.py`. In multi-camera mode it starts
one single-camera launch per camera and passes:

```text
camera_name
load_config_json_file_path
usb_port or serial_number, when configured
```

After streams are stable, the script saves 1 JPG image for each color and depth
image topic by default. Use `--save-image-count` to change the per-topic image
count for each test; set it to `0` to disable image saving.

```bash
--save-image-count 3
```

Default monitored and saved topics:

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

Pass `--image-topic` repeatedly to specify any image topic, such as IR or
left/right IR:

```bash
--image-topic /{camera}/color/image_raw \
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/ir/image_raw
```

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw
```

Then the script calls:

```text
/{camera}/export_config_json
```

Service type:

```text
orbbec_camera_msgs/srv/SetString
```

## Result Files

Each run creates:

```text
export_load_stress_test/results/YYYYMMDD_HHMMSS_export_load/
```

Files:

```text
summary.md            # Final result and per-test status
result.json           # Full machine-readable result
images/               # Saved JPG images per test/camera/topic
exports/              # Exported JSON files and failure diffs per test/camera
```
