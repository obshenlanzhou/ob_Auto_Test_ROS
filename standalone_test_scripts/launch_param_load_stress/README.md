# Launch Param Load Stress

中文文档: [README.zh-CN.md](README.zh-CN.md)

This standalone script launches the Orbbec ROS camera driver with a
`config_file_path` YAML, verifies that every setting takes effect, and can
repeat the whole cycle for stress testing. It supports single-camera and
multi-camera setups under both ROS 1 and ROS 2, and does not depend on the
`orbbec_camera_auto_test` framework.

Each launch cycle is verified at three levels:

- **ROS parameter values** — bulk-queried via `ros2 param dump` / `rosparam get`
- **Image stream topics** — stream-switch keys (`enable_color`, `enable_depth`, …) are verified by receiving (or not receiving) image messages
- **Read-only getter services** — exposure, gain, white balance, laser, LDP, PTP, and point cloud decimation are read back from the device

Startup is detected by monitoring the launch log for `Initialize device cost:`.
Any `[ERROR]`, `[WARN]`, or `[FATAL]` line in the log causes the run to fail
immediately.

## Quick Start

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/orbbec_camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --config-file-path ./config/sample_config_file_path.yaml
```

ROS 1:

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/orbbec_camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --config-file-path ./config/sample_config_file_path.yaml
```

## Stress Test (Repeated Runs)

Use `--repeat N` to launch and verify N times in a row. Each run starts a
fresh launch process and stops it after checks complete.

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --config-file-path ./config/sample_config_file_path.yaml \
  --repeat 20
```

The script prints a per-run pass/fail summary and exits `0` only when every
run passes.

## Multi-Camera

For multi-camera setups, each camera is launched as a separate process with
its own log file. Use `--camera` once per device. Each camera requires a `usb_port` to distinguish devices.

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera1,usb_port=2-1,config_file_path=./config/cam1.yaml \
  --camera camera2,usb_port=2-2,config_file_path=./config/cam2.yaml
```

`--camera` format:

```text
name[,serial_number=SN][,usb_port=X][,config_file_path=/path/to/config.yaml]
```

A shared `--config-file-path` may be given as a fallback for cameras that do
not specify their own `config_file_path`.

## Multi-Camera Stress Test

Combine `--camera` and `--repeat` to run multi-camera verification repeatedly:

```bash
python3 ./launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera1,usb_port=2-1,config_file_path=./config/cam1.yaml \
  --camera camera2,usb_port=2-2,config_file_path=./config/cam2.yaml \
  --repeat 10
```

## Config YAML

An example config is provided at
[`config/sample_config_file_path.yaml`](config/sample_config_file_path.yaml).
Set values that the connected device actually supports. Placeholder values
(`-1`, empty string, `ANY`, `none`, `null`) are skipped at service level
because the driver uses device defaults.

```bash
cp ./config/sample_config_file_path.yaml /tmp/my_test_config.yaml
# Edit values as needed, then pass it to --config-file-path
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--ros-version {1,2}` | `2` | ROS version |
| `--ros-setup PATH` | | ROS environment setup script (e.g. `/opt/ros/humble/setup.bash`) |
| `--driver-setup PATH` | | Camera driver workspace setup script |
| `--launch-package PKG` | `orbbec_camera` | ROS package containing the launch file |
| `--launch-file FILE` | | Launch filename or absolute path (required) |
| `--config-file-path PATH` | | Shared config YAML (required unless each `--camera` has its own) |
| `--camera SPEC` | | Per-camera spec; repeatable for multi-camera mode |
| `--camera-name NAME` | `camera` | Camera name for single-camera mode |
| `--serial-number SN` | | Serial number for single-camera mode |
| `--usb-port PORT` | | USB port for single-camera mode |
| `--launch-arg KEY=VALUE` | | Extra launch argument; repeatable |
| `--startup-timeout SECS` | `30` | Max seconds to wait for `Initialize device cost:` |
| `--topic-timeout SECS` | `20` | Max seconds to wait for each enabled stream topic |
| `--service-timeout SECS` | `15` | Max seconds for each param/service query |
| `--repeat N` | `1` | Number of full test cycles (stress test) |
| `--results-dir PATH` | | Output directory; defaults to `results/<run_id>/` |
| `--skip-topic-check` | | Skip image topic verification |
| `--skip-service-check` | | Skip getter service verification |
| `--save-images-count N` | `0` | Save N images per enabled stream topic per camera (`0` = disabled) |
| `--jpg-quality Q` | `80` | JPEG quality for saved images (1–100) |
| `--show-verification-map` | | Print built-in verification map and exit |

## Startup Detection

The script waits for the line `Initialize device cost:` to appear in the
launch log before running checks. This is more reliable than a fixed sleep
because it reflects actual device initialization completion.

If any `[ERROR]`, `[WARN]`, `[FATAL]`, `process has died`, or Python
`Traceback` line appears before or during startup, the run is aborted
immediately and reported as failed.

## Verification Rules

Full mapping: [VERIFICATION.md](VERIFICATION.md)

- **Parameters**: bulk-queried with `ros2 param dump /<camera>/<camera>` (ROS 2)
  or `rosparam get /<camera>/<camera>` (ROS 1). Falls back to per-parameter
  queries if bulk query fails.
- **Topics**: `enable_color`, `enable_depth`, `enable_ir`, `enable_left_ir`,
  `enable_right_ir`, `enable_left_color`, `enable_right_color` are verified by
  receiving (or not receiving) an image within `--topic-timeout`.
- **Services**: exposure, gain, white balance, laser, LDP, PTP, and point cloud
  decimation are read back from the device when the getter service is
  advertised.

```bash
python3 ./launch_param_load_stress.py --show-verification-map
```

## Output Structure

```text
results/<run_id>/
├── run001/
│   ├── camera1.log        # Launch log for camera1
│   └── camera2.log        # Launch log for camera2 (multi-camera only)
├── run002/
│   └── ...
├── images/                # Present only when --save-images-count > 0
│   ├── run001/
│   │   ├── camera1/
│   │   │   ├── _camera1_color_image_raw/
│   │   │   │   ├── image_0001.jpg
│   │   │   │   └── image_0002.jpg
│   │   │   └── _camera1_depth_image_raw/
│   │   │       └── image_0001.jpg
│   │   └── camera2/
│   │       └── ...
│   └── run002/
│       └── ...
├── summary.md             # Human-readable summary of all runs
└── result.json            # Machine-readable result for all runs
```

- Returns `0` when all runs and all cameras pass
- Returns `1` on any failure
- Returns `130` when interrupted (Ctrl-C)
