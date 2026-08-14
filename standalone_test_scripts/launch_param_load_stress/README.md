# Launch Param Load Stress

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Introduction

Verifies that the YAML config passed via `config_file_path` takes effect on
every launch start. Supports repeated cycles for stress testing and multi-camera
setups.

Each launch cycle is verified at three levels:

```text
ROS parameters  — bulk-queried and compared against the config YAML
Image topics    — stream-enable flags (enable_color, enable_depth, …)
                  verified by receiving or not receiving image messages
Getter services — exposure, gain, white balance, laser, LDP, PTP, and
                  point cloud decimation read back from the device
```

**Limitation**: only a subset of parameters (see [VERIFICATION.md](VERIFICATION.md))
support getter service read-back that reflects actual device state. All other
parameters are verified only by checking that the ROS parameter server was
updated and that the launch produced no errors.

## Usage

### Single Camera

```bash
cd standalone_test_scripts

python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,config-file-path=./config/sample_config_file_path.yaml \
  --run-count 20
```

ROS 1:

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --camera name=camera,config-file-path=./config/sample_config_file_path.yaml \
  --run-count 20
```

### Multi-Camera

Use `--camera` once per device:

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera1,usb-port=2-1,config-file-path=./config/cam1.yaml \
  --camera name=camera2,usb-port=2-2,config-file-path=./config/cam2.yaml \
  --run-count 10
```

`--camera` is a comma-separated `KEY=VALUE` specification. Supported keys are
`name`, `serial-number`, `usb-port`, `device-ip`, `device-port`, and
`config-file-path`; every field is optional. This script requires
`config-file-path` in each camera specification.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` or `2` | ROS version, either `1` or `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` or empty | Path to the ROS environment setup script |
| `--driver-setup` | `$ORBBEC_DRIVER_SETUP`, `$ORBBEC_CAMERA_SETUP`, or empty | Path to the Orbbec driver environment setup script |
| `--launch-file` | required | Launch filename or path |
| `--camera` | required | Camera specification with `config-file-path`; repeatable |
| `--launch-arg` | — | Extra launch argument (e.g. `enable_depth=true`); repeatable, format `KEY=VALUE` or `KEY:=VALUE` |
| `--launch-start-interval SECS` | `2` | Delay in seconds between starting each camera launch (`0` starts all cameras at once) |
| `--run-count N` | `1` | Maximum number of full launch–check–stop cycles |
| `--duration` | empty | Optional maximum wall time; the first configured limit reached stops the run |
| `--startup-timeout SECS` | `30` | Max wait for device initialization |
| `--topic-timeout SECS` | `20` | Max wait for each enabled stream topic |
| `--service-timeout SECS` | `15` | Max wait for each param/service query |
| `--save-image-count N` | `1` | Images saved per selected topic per camera (`0` = disabled) |
| `--image-topic` | auto-discovered | Explicit `Image` or `CompressedImage` topic to save; repeatable, supports `{camera}` |
| `--skip-topic-check` | — | Skip image topic verification |
| `--skip-service-check` | — | Skip getter service verification |

By default, image saving discovers every published `sensor_msgs/Image` stream
under each configured camera namespace. Supplying one or more `--image-topic`
values restricts saving to exactly those topics.
Raw `Image` messages are saved as pixel-lossless PNG files at fixed lossless compression level 1
and retain 16-bit depth values.
`CompressedImage` messages are not decoded or validated; their `data` bytes are written directly
to `.jpg`. Auto-discovery does not include compressed topics.

### Config File

The config YAML specifies which parameter values to load and verify. A sample
file is provided at:

```text
launch_param_load_stress/config/sample_config_file_path.yaml
```

Copy and edit it for the connected device:

```bash
cp ./config/sample_config_file_path.yaml /tmp/my_config.yaml
# Edit values, then pass it as the camera's config-file-path
```

Placeholder values (`-1`, empty string, `ANY`, `none`, `null`) are skipped at
service level — the driver uses device defaults for those.

`--sdk-log-level` controls Orbbec SDK file logging and defaults to `debug`. Supported
values are `debug`, `info`, `warn`, `error`, `fatal`, and `none`.

## Result Files

Each run creates:

```text
launch_param_load_stress/results/YYYYMMDD_HHMMSS_launch_param_load_stress/
├── test_0001/
│   ├── camera1.launch.log      # ROS launch log for camera1
│   ├── camera2.launch.log      # ROS launch log for camera2 (multi-camera only)
│   └── sdk/Log/camera1/
│       └── camera1.log         # Orbbec SDK log for this run
├── test_0002/
│   └── ...
├── images/                # Present only when --save-image-count > 0
│   ├── camera_01/color/image_0001.png
│   ├── camera_01/color/image_0002.jpg
│   ├── camera_01/depth/image_0001.png
│   └── camera_02/ir_left/image_0001.png
├── summary.md             # Per-run pass/fail summary
├── events.jsonl           # Structured lifecycle and progress events
└── result.json            # Machine-readable result for all runs
```

Image numbers continue independently for each camera and stream across test
cycles. Existing images are never overwritten.
