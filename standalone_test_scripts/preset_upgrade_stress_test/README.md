# Preset Upgrade Stress Test

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Introduction

Alternately upgrade two optional depth preset bin files, start the camera launch
with the matching `device_preset` after each update, and verify that image
streams are stable. Optionally saves images per test.

Typical flow per test:

```text
Upgrade preset bin → start launch with matching device_preset
Wait for "Loaded device preset:" in log
Subscribe to image topics → verify streams are stable → save images
Stop launch → wait for `--restart-delay` (2 seconds by default) → switch to next preset → repeat
```

## Usage

### Single Camera

```bash
cd standalone_test_scripts

python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --run-count 10 \
  --save-image-count 1
```

ROS 1:

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --run-count 10
```

### Multi-Camera

Pass `--camera` once per device. Set `usb-port` or `serial-number` to avoid
selecting the wrong device during upgrade or launch:

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --camera name=camera_01,usb-port=2-1 \
  --camera name=camera_02,usb-port=2-3 \
  --run-count 10 \
  --save-image-count 1
```

`--camera` is a comma-separated `KEY=VALUE` specification. Supported keys are
`name`, `serial-number`, `usb-port`, `device-ip`, `device-port`, and
`config-file-path`; every field is optional.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` or `2` | ROS version, either `1` or `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` or empty | Path to the ROS environment setup script |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` or empty | Path to the Orbbec driver environment setup script |
| `--camera` | empty | Camera specification; repeatable, using the format shown above |
| `--preset-a-path` / `--preset-b-path` | bundled bin files | Preset files to upgrade alternately |
| `--preset-a-name` | `K High Confidence` | `device_preset` name corresponding to preset A |
| `--preset-b-name` | `K High Accuracy` | `device_preset` name corresponding to preset B |
| `--run-count` | empty | Optional maximum number of upgrade cycles |
| `--continue-on-failure` | disabled | Clean up a failed preset test and continue with the next one; the final result still fails |
| `--duration` | empty | Maximum wall time; supports `300`, `15m`, and `2h` |
| `--save-image-count` | `1` | Artifacts per topic: image/IMU PNG and point-cloud PLY (`0` = validation only) |
| `--image-topic` | auto-discovered | Explicit `Image` or `CompressedImage` topic to monitor and save; repeatable, supports `{camera}` |
| `--point-cloud-topic` | first-test discovery | Required `PointCloud2` topic; repeatable, supports `{camera}` |
| `--imu-topic` | first-test discovery | Required `Imu` topic; repeatable, supports `{camera}` |
| `--launch-arg` | — | Extra launch argument (e.g. `enable_left_ir=true`); repeatable |
| `--launch-start-interval` | `2` | Delay in seconds between starting each camera launch (`0` starts all cameras at once) |
| `--restart-delay` | `2` | Delay in seconds after launch stops and before switching presets (`0` disables the extra delay) |
| `--sdk-log-level` | `debug` | SDK log level for the preset upgrade tool and camera launch |

At least one of `--run-count` and `--duration` is required. Both may be supplied; the first limit
reached stops the test.

By default, every published `sensor_msgs/Image` stream under each configured
camera namespace is discovered. To restrict the selection, pass
`--image-topic` together with any matching `--launch-arg` values:

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw \
--launch-arg enable_left_ir=true \
--launch-arg enable_right_ir=true
```

`sensor_msgs/Image` messages are saved as pixel-lossless PNG files at fixed lossless compression
level 1, preserving 16-bit depth values. `sensor_msgs/CompressedImage` messages are not decoded or validated; their `data` bytes
are written directly to `.jpg`. Auto-discovery selects only `Image`; compressed topics must be
specified explicitly.

The first successful preset test discovers `PointCloud2` and `Imu` topics
and freezes them as the baseline for all later tests. Point clouds are rendered
as RGB/depth-colored three-view PNGs; each IMU plot contains at least ten valid
samples over at least two seconds. Explicit sensor topic options are mandatory
from the first test.

### Config File

The script alternates between preset A and preset B. Default mapping:

```text
config/g336x_K_High_Confidence_0.0.2.bin → device_preset: K High Confidence
config/g336x_K_High_Accuracy_0.0.2.bin   → device_preset: K High Accuracy
```

To use different presets, pass the path and name explicitly:

```bash
--preset-a-path /path/to/a.bin --preset-a-name "K Clean Medium Confidence" \
--preset-b-path /path/to/b.bin --preset-b-name "K High Accuracy"
```

## Result Files

Each run creates:

```text
preset_upgrade_stress_test/results/YYYYMMDD_HHMMSS_preset_upgrade/
├── summary.md                              # Final summary
├── result.json                             # Machine-readable result
├── events.jsonl                            # Structured lifecycle and progress events
├── logs/test_XXXX/<camera>/upgrade.log     # firmware_update_tool output
├── logs/test_XXXX/<camera>/<camera>.launch.log  # launch output
├── logs/test_XXXX/<camera>/sdk/Log/         # Upgrade-tool and camera SDK debug logs
└── images/                                # Raw PNG and byte-for-byte CompressedImage JPG files
    ├── camera_01/color/image_0001.png
    ├── camera_01/color/image_0002.jpg
    ├── camera_01/depth/image_0001.png
    ├── camera_01/point_cloud_depth/point_cloud_0001.ply
    ├── camera_01/imu_gyro_accel/image_0001.png
    └── camera_02/ir_left/image_0001.png
```

Image numbers continue independently for each camera and stream across test
cycles. Existing images are never overwritten.
