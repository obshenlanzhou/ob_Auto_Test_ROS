# Standalone Test Scripts

中文文档: [README.zh-CN.md](README.zh-CN.md)

This directory contains customer-facing test scripts that can run without the
`orbbec_camera_auto_test` framework. Each script should be self-contained and
should only depend on ROS, the Orbbec camera driver workspace, and standard
Python modules where possible.

## Directory Layout

```text
standalone_test_scripts/
├── README.md
├── README.zh-CN.md
├── export_load_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── export_load_stress_test.py
│   ├── config/
│   └── results/                  # Generated at runtime
├── preset_upgrade_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── preset_upgrade_stress_test.py
│   ├── config/
│   └── results/                  # Generated at runtime
├── firmware_update_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── firmware_update_stress_test.py
│   └── results/                  # Generated at runtime
├── launch_restart_stream_check/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_restart_stream_check.py
│   └── results/                  # Generated at runtime
├── stream_toggle_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── stream_toggle_stress_test.py
│   └── results/                  # Generated at runtime
├── launch_param_load_stress/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_param_load_stress.py
│   ├── config/
│   └── results/                  # Generated at runtime
├── image_receive_stats_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   └── image_topic_receive_stats.py
```

Most stress-test script directories own their generated `results/` directory.
`image_receive_stats_test` writes to the configured output directory instead.

## Environment

Most scripts that launch the camera driver can source ROS and camera driver
environments by command-line options:

```bash
--ros-setup /opt/ros/humble/setup.bash
--driver-setup /path/to/orbbec_camera_ws/install/setup.bash
```

`image_receive_stats_test` is a subscriber-only tool, but accepts the same
environment options so it can be launched consistently.

## Common Command and Result Contract

All public long options use kebab-case. Scripts use `--run-count` for a cycle
limit and `--duration` for a wall-time limit; when both are supplied, the first
limit reached stops the run. A camera is supplied as a repeatable launch-style
specification:

```bash
--camera name=camera_01,serial-number=SN001,usb-port=2-1
```

The supported fields are `name`, `serial-number`, `usb-port`, `device-ip`,
`device-port`, and `config-file-path`. Every field is optional and compatible
fields may be combined. Scripts that do not require explicit camera
configuration provide their own default.

Every completed run writes `result.json`, `summary.md`, and `events.jsonl`.
`result.json` uses the common status values `passed`, `failed`, and
`interrupted`; the corresponding process exit codes are `0`, `1`, and `130`.
Invalid command-line usage returns `2`. Script-specific logs, images, CSV files,
and exports are listed in `result.json` under `artifacts`.

## Local Web UI Integration

Each script directory contains a developer-maintained `ui_manifest.json`.
The local Web UI discovers these manifests and generates basic and advanced
forms without exposing a raw argument field:

```text
http://127.0.0.1:8000/?workspace=standalone
```

The manifest declares field types, defaults, risk level, and stop policy. Keep
the script independent: the manifest may describe its CLI, but the script must
not import the Web UI package.

## Script Index

| Script directory | Purpose | Details |
| --- | --- | --- |
| [launch_restart_stream_check](launch_restart_stream_check/README.md) | Repeatedly restart a launch file and check image stream recovery | Launch restart stream stability stress test |
| [stream_toggle_stress_test](stream_toggle_stress_test/README.md) | Toggle individual/all streams, optionally alternate two resolution/FPS/format sets, and verify recovery | Single/multi-camera with raw PNG and byte-for-byte compressed JPG evidence; individual/profile switching supports ROS1/ROS2, all mode is currently ROS2 only |
| [launch_param_load_stress](launch_param_load_stress/README.md) | Stress-test launch parameter loading via `config_file_path` | Verifies ROS parameters, image topics, and getter services; supports multi-camera and repeated runs |
| [export_load_stress_test](export_load_stress_test/README.md) | Alternate JSON import/export and compare parameters | Config JSON import/export consistency stress test |
| [preset_upgrade_stress_test](preset_upgrade_stress_test/README.md) | Alternately update optional depth presets and verify streams | Optional depth preset upgrade stress test |
| [firmware_update_stress_test](firmware_update_stress_test/README.md) | Repeatedly call `firmware_update_tool --firmware_path` and check success logs | Firmware update command stress test; supports serial-number batch updates |
| [image_receive_stats_test](image_receive_stats_test/README.md) | Subscribe image topics and record receive-gap statistics | ROS1/ROS2 subscriber-side stream stall and timestamp monitor |

## Adding New Standalone Scripts

When adding a new script:

```text
Put each test script in its own directory
Include README.md and README.zh-CN.md in the script directory
Include a ui_manifest.json when the script should appear in the local Web UI
Use a clear name that describes the test scenario
Keep it independent from orbbec_camera_auto_test framework modules
Support --ros-version, --ros-setup, --driver-setup when ROS is needed
Accept the common camera, lifecycle, and environment options when applicable
Write result.json, summary.md, and events.jsonl using the common contract
Return 0 for pass, 1 for failure, 130 for interruption, and 2 for invalid arguments
```
