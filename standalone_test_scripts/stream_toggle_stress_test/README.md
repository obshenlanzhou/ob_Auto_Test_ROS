# Stream Toggle Stress Test

Chinese: [README.zh-CN.md](README.zh-CN.md)

## Overview

This tool starts one ROS launch process and supports two configurable stream-toggle modes:

- `--toggle-mode individual` (default) disables and restores one `toggle_<stream>` at a time.
- `--toggle-mode all` uses each camera's `set_streams_enable` service to stop and start all streams.

It supports a single-camera launch and a preconfigured multi-camera launch. ROS-version support is:

| Toggle mode | ROS 1 | ROS 2 |
| --- | --- | --- |
| `individual` per-stream toggle | Supported | Supported |
| `all` whole-camera toggle | **Not supported** | Supported |

The current ROS1 v2.9.3 driver provides only the per-stream `toggle_<stream>` services and does not
provide the `set_streams_enable` service required by whole-camera mode. ROS1 must therefore use the
default `individual` mode. If `--toggle-mode all` is requested with ROS1, preflight fails clearly
before the stress run begins. ROS2 `all` mode requires every target camera to advertise
`/<camera_name>/set_streams_enable`.

In individual mode, each target stream runs this transaction:

```text
disable target
  → target stays quiet for 2 s while every other selected stream is stable for 5 s
  → enable target
  → every selected stream is stable for 5 s
  → save an image from the restored target
```

In all mode, each cycle runs this transaction:

```text
call set_streams_enable(false) for every target camera
  → every target image stream stays quiet for 2 s
  → call set_streams_enable(true) for every target camera
  → every target image stream is stable for 5 s
  → save an image from every target stream
```

Multi-camera services are called in deterministic camera-namespace order. One all-stream service
affects every launch-enabled stream on that camera. Explicit `--image-topic` values select the
verification and image-evidence list, but the all-stream service still affects all enabled streams
on that camera.

The driver rebuilds its pipeline when toggling a stream. The tool therefore monitors all selected
streams across every camera for collateral failures. Individual mode operates one stream at a time
in camera-namespace and stream-name order; all mode operates one camera at a time in namespace
order.

## Usage

### Single camera

```bash
cd standalone_test_scripts

python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,usb-port=2-1 \
  --duration 1h
```

All-stream mode:

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera name=camera,usb-port=2-1 \
  --toggle-mode all \
  --run-count 10
```

ROS 1:

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --camera name=camera,usb-port=2-1 \
  --run-count 10
```

The ROS1 example uses the default `--toggle-mode individual`. The current ROS1 v2.9.3 driver does
not support `--toggle-mode all`.

At most one `--camera` may be supplied. It accepts the common standalone fields `name`,
`serial-number`, `usb-port`, `device-ip`, `device-port`, and `config-file-path`, which are
forwarded as single-camera launch arguments. With no `--camera`, no camera-specific arguments are
injected and the launch defaults are used.

### Multi-camera launch

Use a launch file in which camera names, device selectors, enabled streams, SDK log levels, and SDK
log names are already configured. The tool starts that launch once; do not repeat `--camera`:

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file /path/to/multi_camera.launch.py \
  --duration 1h
```

By default, the tool discovers `sensor_msgs/Image` topics and selects only streams with a matching
`std_srvs/SetBool` service:

```text
/camera_01/color/image_raw   → /camera_01/toggle_color
/camera_02/left_ir/image_raw → /camera_02/toggle_left_ir
```

Derived Image topics such as `depth_to_color` or `confidence` are skipped when they do not have a
toggle service, and the skip is recorded. Repeat `--image-topic` to provide a strict target list:

```bash
python3 ./stream_toggle_stress_test/stream_toggle_stress_test.py \
  --launch-file /path/to/multi_camera.launch.py \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_01/depth/image_raw \
  --image-topic /camera_02/color/image_raw \
  --image-topic /camera_02/depth/image_raw \
  --run-count 10
```

Explicit topics must match `/<camera-namespace>/<stream>/image_raw`, use
`sensor_msgs/Image`, and advertise the corresponding `toggle_<stream>` service. Otherwise,
preflight fails. The `{camera}` placeholder is supported only when one `--camera` is provided.

## Cycles, retries, and stopping

In individual mode, one complete cycle toggles and verifies every target stream once. In all mode,
one cycle stops every target camera, verifies all target streams are quiet, starts every target
camera, verifies recovery, and saves per-stream evidence. When both `--run-count` and
`--duration` are set, the first limit reached stops the run. The first full cycle always completes
so that every target is tested. In later cycles, an expired duration stops the run after the
current per-stream transaction has restored the stream.

A service call times out after 15 seconds by default. A failed first call is retried once after one
second:

- A successful retry continues the run and records a warning while preserving a final `passed`
  status.
- Two failures stop the test immediately after a best-effort target-stream restore.
- Ctrl+C or a UI stop also attempts restoration and short image confirmation, then returns
  `interrupted` with exit code 130.

## Main options

| Option | Default | Description |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` or `2` | ROS version, `1` or `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` or empty | ROS setup script |
| `--driver-setup` | `$ORBBEC_DRIVER_SETUP` / `$ORBBEC_CAMERA_SETUP` or empty | Driver setup script |
| `--launch-package` | `orbbec_camera` | Launch package |
| `--launch-file` | required | Launch filename or path |
| `--launch-arg` | — | Extra launch argument; repeatable |
| `--camera` | empty | Single-camera launch arguments; at most one |
| `--image-topic` | auto | Strict raw-image target; repeatable |
| `--toggle-mode` | `individual` | `individual` per-stream toggles (ROS1/ROS2); `all` whole-camera toggles (currently ROS2 only) |
| `--duration` | `300` | Maximum duration; supports `15m` and `2h` |
| `--run-count` | empty | Maximum completed cycles |
| `--topic-discovery-timeout` | `30` | Topic/service discovery timeout |
| `--topic-discovery-settle` | `2` | No-new-target discovery window |
| `--stop-stable-seconds` | `2` | Target quiet confirmation window |
| `--stable-seconds` | `5` | Continuous recovery stability window |
| `--stream-timeout` | `20` | Disabled/enabled state timeout |
| `--max-gap-seconds` | `1.5` | Maximum receive gap in a stable window |
| `--service-timeout` | `15` | Toggle service-call timeout |
| `--service-retry-delay` | `1` | Delay before the one retry |
| `--save-image-count` | `1` | JPG files per stream per cycle; `0` disables saving |
| `--save-image-timeout` | `30` | Per-stream image-save timeout |
| `--jpg-quality` | `95` | JPG quality from 1 to 100 |
| `--sdk-log-level` | `debug` | SDK log level for a single-camera launch; preconfigure it in a multi-camera launch |
| `--queue-size` | `10` | Image subscription queue size |
| `--results-dir` | generated | Custom result directory |

Image saving requires `cv_bridge` and OpenCV. Dependencies are checked before launch startup when
`--save-image-count` is greater than zero; use `0` to disable image saving.

## Results

```text
stream_toggle_stress_test/results/YYYYMMDD_HHMMSS_stream_toggle/
├── logs/
│   ├── camera.launch.log
│   └── sdk/
├── images/
│   ├── camera_01/color/image_0001.jpg
│   └── camera_02/depth/image_0001.jpg
├── summary.md
├── events.jsonl
└── result.json
```

`result.json` records every cycle, service attempt and retry, disabled/recovered timing, frame and
gap metrics, cleanup result, and image path. Unified standalone statuses and exit codes are
`passed`/0, `failed`/1, and `interrupted`/130. Invalid CLI arguments return 2.
