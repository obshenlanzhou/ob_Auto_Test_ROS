# Firmware Update Stress Test

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Introduction

Repeatedly call the Orbbec driver `firmware_update_tool` with one or more
firmware files. The script saves every update log and marks a test as passed
only when both conditions are met:

```text
firmware_update_tool exits with code 0
the log contains: Firmware tool completed successfully. Updated X/Y target device(s).
```

No firmware version check, device discovery, launch startup, or stream check is
performed by this tool.

Typical flow per test:

```text
Pick next firmware from list -> call firmware_update_tool
Mirror terminal output to logs/test_XXXX/update.log
Check return code and success log -> repeat
```

## Usage

### ROS 2

```bash
cd standalone_test_scripts

python3 ./firmware_update_stress_test/firmware_update_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --firmware /path/to/firmware_A.bin \
  --firmware /path/to/firmware_B.bin \
  --test-count 10
```

### ROS 1

```bash
python3 ./firmware_update_stress_test/firmware_update_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --firmware /path/to/firmware_A.bin \
  --test-count 10
```

### Multi-Camera Batch by Serial Number

Multiple serial numbers are passed to one `firmware_update_tool` invocation as
a comma-separated batch:

```bash
python3 ./firmware_update_stress_test/firmware_update_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --serial-number SN001,SN002,SN003 \
  --firmware /path/to/firmware_A.bin \
  --firmware /path/to/firmware_B.bin \
  --test-count 6
```

`--serial-number` can also be repeated:

```bash
--serial-number SN001 --serial-number SN002
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--firmware` | required | Firmware image path; repeat to cycle files in order |
| `--test-count` | `10` | Number of update command invocations; `0` = run until `--duration` |
| `--duration` | `300` | Duration for `--test-count 0`; supports `300`, `15m`, `2h` |
| `--restart-delay` | `2` | Delay seconds between update commands |
| `--serial-number` | empty | Target serial number(s); repeatable or comma-separated |
| `--usb-port` | empty | Single target USB port selector |
| `--device-ip` | empty | Single target network device IP selector |
| `--device-port` | `8090` | Network device port passed to the firmware tool |
| `--reconnect-timeout-sec` | `120` | Passed to `firmware_update_tool` |
| `--reconnect-poll-ms` | `1000` | Passed to `firmware_update_tool` |
| `--sdk-log-level` | `off` | Passed to `firmware_update_tool` |
| `--continue-on-error` | disabled | Passed to `firmware_update_tool` |

Only one selector type can be used at a time: serial number, USB port, or device
IP. Multi-camera mode is serial-number batch mode.

## Result Files

Each run creates:

```text
firmware_update_stress_test/results/YYYYMMDD_HHMMSS_firmware_update/
├── summary.md                  # Final result and per-test pass/fail status
├── result.json                 # Full machine-readable result
└── logs/test_XXXX/update.log   # firmware_update_tool terminal output
```
