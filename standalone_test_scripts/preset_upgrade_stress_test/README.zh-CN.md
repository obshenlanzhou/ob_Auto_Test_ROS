# Preset 升级压测

English: [README.md](README.md)

## 用途

```text
交替升级两份 optional depth preset bin 文件，升级成功后启动相机 launch，
传入当前升级文件对应的 device_preset，检查 launch 日志确认 preset 已加载，
再验证 color / depth 图像流是否正常，可选择保存图像；也支持指定任意
sensor_msgs/Image 图像流。
```

## 基本用法

单相机：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts

python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --test-count 10 \
  --save-images-count 1
```

多相机：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros2_v2-main/install/setup.bash \
  --camera camera_01,usb_port=2-1 \
  --camera camera_02,usb_port=2-3 \
  --test-count 10 \
  --save-images-count 1
```

ROS1：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /home/slz/ORBBEC/orbbecsdk_ros1_v2-main/devel/setup.bash \
  --test-count 10
```

## 多相机

多相机时重复传入 `--camera`。格式：

```text
--camera name[,usb_port=PORT][,serial_number=SN]
```

每台相机建议配置 `usb_port` 或 `serial_number`，避免升级或 launch 选错设备。
每次 preset 测试会先逐台相机升级 preset，再分别启动每台相机的单相机 launch，
最后统一验证所有相机的图像流。

## Preset 映射

脚本需要同时知道：

```text
preset path：要升级的 bin 文件路径
preset name：升级后启动 launch 时传给 device_preset 的名称
```

默认映射：

```text
preset_upgrade_stress_test/config/g336x_K_High_Confidence_0.0.2.bin -> K High Confidence
preset_upgrade_stress_test/config/g336x_K_High_Accuracy_0.0.2.bin   -> K High Accuracy
```

如果修改了 preset 文件或名称，请显式传入：

```bash
python3 ./preset_upgrade_stress_test/preset_upgrade_stress_test.py \
  --preset-a-path /path/to/a.bin \
  --preset-a-name "K Clean Medium Confidence" \
  --preset-b-path /path/to/b.bin \
  --preset-b-name "K High Accuracy"
```

每次升级成功后，launch 会传入当前 preset 名称，例如：

```text
device_preset:=K High Confidence
```

并等待 launch 日志出现：

```text
Loaded device preset: K High Confidence
```

## 压测次数

```bash
--test-count 10
```

表示运行 10 轮，每轮包含 preset A 和 preset B 各一次升级、launch、出流验证。

```bash
--test-count 0
```

表示不按次数限制，持续运行到 `--duration` 到期或用户按 `Ctrl+C` 中断。

## 存图

```bash
--save-images-count 1
```

表示每次升级后，每个监控 topic 保存 1 张 JPG 图像，存图方式与
`export_load_stress_test` 一致。

设置为 `0` 表示只验证出流，不保存图像：

```bash
--save-images-count 0
```

JPG 质量默认是 95，可以通过以下参数调整：

```bash
--jpg-quality 95
```

默认监控 topic：

```text
/{camera}/color/image_raw
/{camera}/depth/image_raw
```

多相机时 `{camera}` 会分别展开为每个 `--camera` 的名称。

可以重复传入 `--image-topic` 覆盖为任意图像流：

```bash
--image-topic /{camera}/depth/image_raw \
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw
```

如果指定的图像流不是 launch 默认打开的流，需要同时传入对应 launch 参数，例如：

```bash
--image-topic /{camera}/left_ir/image_raw \
--image-topic /{camera}/right_ir/image_raw \
--launch-arg enable_left_ir=true \
--launch-arg enable_right_ir=true
```

## 结果文件

每次运行会创建结果目录：

```text
preset_upgrade_stress_test/results/YYYYMMDD_HHMMSS_preset_upgrade/
```

文件说明：

```text
summary.md                              # 最终摘要
result.json                             # 完整机器可读结果
logs/test_XXXX/<camera>/upgrade.log     # firmware_update_tool 输出
logs/test_XXXX/<camera>/launch.log      # launch 输出
images/test_XXXX/<camera>/              # 保存的 JPG 图像，禁用存图时为空或不存在
```

退出码：

```text
0    测试通过
1    测试失败
130  用户中断
```
