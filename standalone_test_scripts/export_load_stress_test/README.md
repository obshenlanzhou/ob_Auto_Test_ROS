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
  --camera name=camera \
  --run-count 10
```

### Multi-Camera

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

`--camera` is a comma-separated `KEY=VALUE` specification. Supported keys are
`name`, `serial-number`, `usb-port`, `device-ip`, `device-port`, and
`config-file-path`; every field is optional.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` or `2` | ROS version, either `1` or `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` or empty | Path to the ROS environment setup script |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` or empty | Path to the Orbbec driver environment setup script |
| `--launch-file` | `gemini_330_series_sdk_json.launch.py` | Launch filename or path |
| `--camera` | empty | Camera specification; repeatable, using the format shown above |
| `--launch-arg` | — | Extra launch argument (e.g. `enable_ir=true`); repeatable, format `KEY=VALUE` or `KEY:=VALUE` |
| `--launch-start-interval` | `2` | Delay in seconds between starting each camera launch (`0` starts all cameras at once) |
| `--run-count` | empty | Maximum number of import/export cycles |
| `--duration` | empty | Optional maximum wall time; the first configured limit reached stops the run |
| `--sdk-log-level` | `debug` | Orbbec SDK log level |
| `--save-image-count` | `1` | Images saved per topic per test (`0` = disabled) |
| `--image-topic` | auto-discovered | Explicit `Image` or `CompressedImage` topic to monitor and save; repeatable |
| `--config-json` | see Config File | JSON files to alternate; repeatable |

At least one of `--run-count` and `--duration` is required. Both may be supplied; the first limit
reached stops the test.

By default, every published `sensor_msgs/Image` stream under each configured
camera namespace is discovered. Pass `--image-topic` repeatedly to restrict
monitoring and saving to an explicit set:

```bash
--image-topic /{camera}/color/image_raw \
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/ir/image_raw
```

`sensor_msgs/Image` messages are saved as pixel-lossless PNG files at fixed lossless compression
level 1, preserving 16-bit depth values. `sensor_msgs/CompressedImage` messages are not decoded or validated; their `data` bytes
are written directly to `.jpg`. Auto-discovery selects only `Image`; compressed topics must be
specified explicitly with `--image-topic`.

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
├── events.jsonl     # Structured lifecycle and progress events
├── images/          # Raw PNG and byte-for-byte CompressedImage JPG files
│   ├── camera_01/color/image_0001.png
│   ├── camera_01/color/image_0002.jpg
│   ├── camera_01/depth/image_0001.png
│   └── camera_02/ir_left/image_0001.png
├── exports/         # Exported JSON and failure diffs per test/camera
├── logs/test_XXXX/<camera>/<camera>.launch.log  # Per-test ROS launch log
└── logs/test_XXXX/<camera>/sdk/Log/<camera>/  # Per-test camera SDK debug logs
```

Image numbers continue independently for each camera and stream across test
cycles. Existing images are never overwritten.
