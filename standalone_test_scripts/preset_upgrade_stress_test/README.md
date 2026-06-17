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
Stop launch → switch to next preset → repeat
```

## Usage

### Single Camera

```bash
cd standalone_test_scripts

python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --test-count 10 \
  --save-images-count 1
```

ROS 1:

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --test-count 10
```

### Multi-Camera

Pass `--camera` once per device. Set `usb_port` or `serial_number` to avoid
selecting the wrong device during upgrade or launch:

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

`--camera` format: `name[,usb_port=PORT][,serial_number=SN]`

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--test-count` | — | Number of upgrade cycles; `0` = run until `--duration` or Ctrl-C |
| `--duration` | — | Time limit (e.g. `1h`), used when `--test-count 0` |
| `--save-images-count` | `0` | Images saved per topic per test (`0` = disabled) |
| `--jpg-quality` | `95` | JPEG quality for saved images (1–100) |
| `--image-topic` | color + depth | Topics to monitor; repeatable, `{camera}` expands to each camera name |
| `--launch-arg` | — | Extra launch argument (e.g. `enable_left_ir=true`); repeatable |

Default monitored topics:

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

To monitor non-default streams, pass both `--image-topic` and the matching
`--launch-arg`:

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw \
--launch-arg enable_left_ir=true \
--launch-arg enable_right_ir=true
```

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
├── logs/test_XXXX/<camera>/upgrade.log     # firmware_update_tool output
├── logs/test_XXXX/<camera>/launch.log      # launch output
└── images/test_XXXX/<camera>/             # Saved JPG images (when enabled)
```
