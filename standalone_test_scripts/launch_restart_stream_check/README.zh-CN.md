# Launch 重启出流检查

English: [README.md](README.md)

## 用途

```text
反复重启一个 ROS launch 文件，并检查每次重启后图像流是否能够恢复且稳定出流。
```

典型流程：

```text
启动 launch
自动发现或使用手动配置的 image topic
订阅 sensor_msgs/Image topic
等待所有监控流稳定出流
关闭 launch
等待 restart delay
重复执行直到 duration 结束
```

## 基本用法

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts

python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --launch-file multi_camera.launch.py \
  --duration 1h
```

使用 launch 文件路径：

```bash
python3 ./launch_restart_stream_check/launch_restart_stream_check.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file /path/to/multi_camera.launch.py \
  --duration 1h
```

## 指定监测 Topic

如果不传 `--image-topic`，脚本只会在第一次 launch 启动后自动发现所有
`sensor_msgs/Image` topic。发现到的 topic 列表会固定下来，后续重启轮次都会检查这份固定列表。

手动指定 topic：

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

## 稳定出流判断规则

一次重启 attempt 只有在所有监控的 image topic 都满足以下条件时才算通过：

```text
连续接收时间 >= --stable-seconds
任意两帧接收间隔不超过 --max-gap-seconds
```

默认值：

```text
--stable-seconds 5
--stream-timeout 20
--max-gap-seconds 1.5
--restart-delay 2
--topic-discovery-timeout 15
```

如果任意一个被监控 topic 在 `--stream-timeout` 内没有达到稳定出流，本轮 attempt
判定失败。脚本会先保留当前 launch 现场，提示人工检查是否仍在出流；人工检查完成后
按 `Ctrl+C`，脚本会停止 launch 并写入 summary，summary 中状态仍为 `failed`。

## 结果文件

每次运行会创建一个结果目录：

```text
launch_restart_stream_check/results/YYYYMMDD_HHMMSS_restart_stream/
```

文件说明：

```text
summary.md            # 运行命令、最终结果、运行时长和检测流列表
```

退出码：

```text
0    测试通过
1    测试失败
130  用户中断
```
