# Launch Restart Stream Check

中文文档: [README.zh-CN.md](README.zh-CN.md)

## Purpose

```text
Repeatedly restart a ROS launch file and verify that image streams recover and
remain stable after each restart.
```

Typical flow:

```text
Start launch
Discover or use configured image topics
Subscribe to sensor_msgs/Image topics
Wait until all monitored streams are stable
Stop launch
Wait restart delay
Repeat until duration ends
```

## Basic ROS2 Usage

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts

python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file multi_camera.launch.py \
  --duration 1h
```

With a launch file path:

```bash
python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file /path/to/multi_camera.launch.py \
  --duration 1h
```

## Topic Selection

If `--image-topic` is not provided, the script auto-discovers all
`sensor_msgs/Image` topics during the first launch attempt. The discovered
topic list is then fixed and reused for later restart attempts.

Manual topic selection:

```bash
python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file multi_camera.launch.py \
  --image-topic /camera_01/color/image_raw \
  --image-topic /camera_02/color/image_raw \
  --duration 1h
```

## Stability Rule

A restart attempt passes only when every monitored image topic satisfies all
conditions:

```text
At least one valid Image message is received
Image width > 0 and height > 0
Continuous receiving time >= --stable-seconds
No receive gap exceeds --max-gap-seconds
```

Default values:

```text
--stable-seconds 5
--stream-timeout 20
--max-gap-seconds 1.5
--restart-delay 2
--topic-discovery-timeout 15
```

If any monitored topic does not become stable within `--stream-timeout`, the
attempt is marked as failed. The script keeps the current launch running for
manual confirmation. Press `Ctrl+C` after manual confirmation; the script stops
the launch, writes the summary, and the summary status remains `failed`.

## Result Files

Each run creates a result directory:

```text
launch_restart_stream_check/results/YYYYMMDD_HHMMSS_restart_stream/
```

Files:

```text
summary.md            # Run command, final result, elapsed time, and monitored streams
```

Exit codes:

```text
0    Passed
1    Failed
130  Interrupted by user
```
