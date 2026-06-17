# Launch Param Load Stress

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Introduction

Load a YAML config file into the Orbbec ROS camera driver via `config_file_path`
and verify that every setting takes effect. Supports repeated cycles for stress
testing and multi-camera setups.

Each launch cycle is verified at three levels:

```text
ROS parameters  — bulk-queried and compared against the config YAML
Image topics    — stream-enable flags (enable_color, enable_depth, …)
                  verified by receiving or not receiving image messages
Getter services — exposure, gain, white balance, laser, LDP, PTP, and
                  point cloud decimation read back from the device
```

## Usage

### Single Camera

```bash
cd standalone_test_scripts

python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --config-file-path ./config/sample_config_file_path.yaml
```

ROS 1:

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --config-file-path ./config/sample_config_file_path.yaml
```

### Multi-Camera and Stress Test

Use `--camera` once per device and `--repeat N` to run multiple cycles:

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera1,usb_port=2-1,config_file_path=./config/cam1.yaml \
  --camera camera2,usb_port=2-2,config_file_path=./config/cam2.yaml \
  --repeat 10
```

`--camera` format: `name[,serial_number=SN][,usb_port=X][,config_file_path=/path]`

A shared `--config-file-path` may be used as a fallback for cameras without a
per-camera `config_file_path`.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--repeat N` | `1` | Number of full launch–check–stop cycles |
| `--startup-timeout SECS` | `30` | Max wait for device initialization |
| `--topic-timeout SECS` | `20` | Max wait for each enabled stream topic |
| `--service-timeout SECS` | `15` | Max wait for each param/service query |
| `--save-images-count N` | `0` | Images saved per enabled topic per camera (`0` = disabled) |
| `--jpg-quality Q` | `80` | JPEG quality for saved images (1–100) |
| `--skip-topic-check` | — | Skip image topic verification |
| `--skip-service-check` | — | Skip getter service verification |

### Config File

The config YAML specifies which parameter values to load and verify. A sample
file is provided at:

```text
launch_param_load_stress/config/sample_config_file_path.yaml
```

Copy and edit it for the connected device:

```bash
cp ./config/sample_config_file_path.yaml /tmp/my_config.yaml
# Edit values, then pass it to --config-file-path
```

Placeholder values (`-1`, empty string, `ANY`, `none`, `null`) are skipped at
service level — the driver uses device defaults for those.

## Result Files

Each run creates:

```text
launch_param_load_stress/results/YYYYMMDD_HHMMSS_launch_param_load_stress/
├── run001/
│   ├── camera1.log        # Launch log for camera1
│   └── camera2.log        # Launch log for camera2 (multi-camera only)
├── run002/
│   └── ...
├── images/                # Present only when --save-images-count > 0
│   ├── run001/
│   │   └── camera1/<topic>/image_0001.jpg
│   └── run002/
│       └── ...
├── summary.md             # Per-run pass/fail summary
└── result.json            # Machine-readable result for all runs
```
