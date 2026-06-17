# JSON Import/Export Stress Test

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Introduction

Alternately load two config JSON files, start the camera launch, wait for
streams to become stable, save images, export the current JSON via service, and
compare only the `parameters` field to verify that settings took effect.

Typical flow per test:

```text
Load config JSON → start launch per camera
Wait for all streams to become stable → save images
Export JSON via service → compare parameters field with imported JSON
Stop launch → switch to next config → repeat
```

## Usage

### Single Camera

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

### Multi-Camera

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

`--camera` format: `name[,usb_port=PORT][,serial_number=SN]`

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--test-count` | `10` | Number of import/export cycles |
| `--save-image-count` | `1` | Images saved per topic per test (`0` = disabled) |
| `--image-topic` | color + depth | Topics to monitor and save; repeatable |
| `--config-json` | see Config File | JSON files to alternate; repeatable |

Default monitored topics:

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

Pass `--image-topic` repeatedly to monitor additional streams:

```bash
--image-topic /{camera}/color/image_raw \
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/ir/image_raw
```

### Config File

By default the script alternates between two bundled JSON files:

```text
export_load_stress_test/config/Gemini_336L_1.json
export_load_stress_test/config/Gemini_336L_2.json
```

To use different files, pass `--config-json` repeatedly in the desired order:

```bash
--config-json /path/to/config_A.json \
--config-json /path/to/config_B.json
```

## Result Files

Each run creates:

```text
export_load_stress_test/results/YYYYMMDD_HHMMSS_export_load/
├── summary.md       # Final result and per-test pass/fail status
├── result.json      # Full machine-readable result
├── images/          # Saved JPG images per test/camera/topic
└── exports/         # Exported JSON and failure diffs per test/camera
```
