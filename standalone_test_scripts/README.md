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
├── launch_restart_stream_check/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_restart_stream_check.py
│   └── results/                  # Generated at runtime
```

Each script directory owns its generated `results/` directory.

## Environment

Scripts can source ROS and camera driver environments by command-line options:

```bash
--ros-setup /opt/ros/humble/setup.bash
--driver-setup /path/to/orbbec_camera_ws/install/setup.bash
```

## Script Index

| Script directory | Purpose | Details |
| --- | --- | --- |
| [launch_restart_stream_check](launch_restart_stream_check/README.md) | Repeatedly restart a launch file and check image stream recovery | Launch restart stream stability stress test |
| [export_load_stress_test](export_load_stress_test/README.md) | Alternate JSON import/export and compare parameters | Config JSON import/export consistency stress test |

## Adding New Standalone Scripts

When adding a new script:

```text
Put each test script in its own directory
Include README.md and README.zh-CN.md in the script directory
Use a clear name that describes the test scenario
Keep it independent from orbbec_camera_auto_test framework modules
Support --ros-version, --ros-setup, --driver-setup when ROS is needed
Write the final result into a script-specific summary.md
Return 0 for pass and non-zero for failure
```
