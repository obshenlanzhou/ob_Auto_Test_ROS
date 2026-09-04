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
  --run-count 10
```

### ROS 1

```bash
python3 ./firmware_update_stress_test/firmware_update_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --firmware /path/to/firmware_A.bin \
  --run-count 10
```

### Multi-Camera Batch by Serial Number

Multiple serial numbers are passed to one `firmware_update_tool` invocation as
a comma-separated batch:

```bash
python3 ./firmware_update_stress_test/firmware_update_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --camera name=camera_01,serial-number=SN001 \
  --camera name=camera_02,serial-number=SN002 \
  --camera name=camera_03,serial-number=SN003 \
  --firmware /path/to/firmware_A.bin \
  --firmware /path/to/firmware_B.bin \
  --run-count 6
```

Each `--camera` is a comma-separated `KEY=VALUE` specification. Supported keys
are `name`, `serial-number`, `usb-port`, `device-ip`, `device-port`, and
`config-file-path`; every field is optional.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` or `2` | ROS version, either `1` or `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` or empty | Path to the ROS environment setup script |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` or empty | Path to the Orbbec driver environment setup script |
| `--firmware` | required | Firmware image path; repeat to cycle files in order |
| `--run-count` | empty | Maximum number of update command invocations |
| `--continue-on-failure` | disabled | Record a failed update cycle and continue with the next one; the final result still fails |
| `--duration` | empty | Optional maximum wall time; supports `300`, `15m`, `2h` |
| `--restart-delay` | `2` | Delay seconds between update commands |
| `--camera` | default camera | Camera target specification; repeatable |
| `--reconnect-timeout-sec` | `120` | Passed to `firmware_update_tool` |
| `--reconnect-poll-ms` | `1000` | Passed to `firmware_update_tool` |
| `--sdk-log-level` | `debug` | Passed to `firmware_update_tool` |
| `--continue-on-error` | disabled | Passed to `firmware_update_tool`; does not control stress-test cycle continuation |

At least one of `--run-count` and `--duration` is required. Both may be supplied; the first limit
reached stops the test.

Compatible selectors in a camera specification may be combined. Multiple
serial numbers are sent as one firmware-tool batch; USB and network selectors
must resolve to one target value.

## Result Files

Each run creates:

```text
firmware_update_stress_test/results/YYYYMMDD_HHMMSS_firmware_update_v2.1.0/
├── summary.md                  # Final result and per-test pass/fail status
├── result.json                 # Full machine-readable result
├── events.jsonl                # Structured lifecycle and progress events
├── logs/test_XXXX/update.log   # firmware_update_tool terminal output
└── logs/test_XXXX/sdk/Log/     # Per-test firmware_update_tool SDK debug logs
```
