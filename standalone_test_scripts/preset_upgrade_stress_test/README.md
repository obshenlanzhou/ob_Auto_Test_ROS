# Preset Upgrade Stress Test

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Purpose

```text
Alternately update two optional depth preset bin files, start the camera launch
with the matching device_preset after each successful update, verify the launch
log, and then check color / depth image streams. Image saving is optional, and
any sensor_msgs/Image stream can be selected explicitly.
```

## Basic Usage

Single camera:

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts

python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --test-count 10 \
  --save-images-count 1
```

Multiple cameras:

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --camera camera_01,usb_port=2-1 \
  --camera camera_02,usb_port=2-3 \
  --test-count 10 \
  --save-images-count 1
```

ROS1:

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros1_v2-main/devel/setup.bash \
  --test-count 10
```

## Multiple Cameras

Pass repeated `--camera` options. Format:

```text
--camera name[,usb_port=PORT][,serial_number=SN]
```

Set `usb_port` or `serial_number` for every camera to avoid selecting the wrong
device during update or launch. For each preset test, the script updates every
camera sequentially, starts one single-camera launch per camera, and then
verifies all configured image streams together.

## Preset Mapping

The script needs both values:

```text
preset path: the bin file to update
preset name: the device_preset value passed to launch after that update
```

Default mapping:

```text
preset_upgrade_stress_test/config/g336x_K_High_Confidence_0.0.2.bin -> K High Confidence
preset_upgrade_stress_test/config/g336x_K_High_Accuracy_0.0.2.bin   -> K High Accuracy
```

If you change preset files or names, pass the mapping explicitly:

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --preset-a-path /path/to/a.bin \
  --preset-a-name "K Clean Medium Confidence" \
  --preset-b-path /path/to/b.bin \
  --preset-b-name "K High Accuracy"
```

After each update, launch receives the current preset name, for example:

```text
device_preset:=K High Confidence
```

The script waits for this launch log:

```text
Loaded device preset: K High Confidence
```

## Test Count

```bash
--test-count 10
```

Runs 10 rounds. Each round includes one preset A update and one preset B update.

```bash
--test-count 0
```

Runs until `--duration` expires or the user presses `Ctrl+C`.

## Image Saving

```bash
--save-images-count 1
```

Saves 1 JPG image per monitored topic after each preset update, using the same
image saving path as `export_load_stress_test`.

Set it to `0` to verify streams without saving images:

```bash
--save-images-count 0
```

Default JPG quality is 95 and can be changed with:

```bash
--jpg-quality 95
```

Default monitored topics:

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

In multi-camera mode, `{camera}` expands to each `--camera` name.

Override with repeated `--image-topic` options to monitor any image stream:

```bash
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw
```

If the selected stream is not enabled by the launch defaults, pass the matching
launch arguments as well, for example:

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw \
--launch-arg enable_left_ir=true \
--launch-arg enable_right_ir=true
```

## Results

Each run creates:

```text
preset_upgrade_stress_test/results/YYYYMMDD_HHMMSS_preset_upgrade/
```

Files:

```text
summary.md                              # Final summary
result.json                             # Machine-readable result
logs/test_XXXX/<camera>/upgrade.log     # firmware_update_tool output
logs/test_XXXX/<camera>/launch.log      # launch output
images/test_XXXX/<camera>/              # Saved JPG images, if enabled
```

Exit codes:

```text
0    Passed
1    Failed
130  Interrupted by user
```
