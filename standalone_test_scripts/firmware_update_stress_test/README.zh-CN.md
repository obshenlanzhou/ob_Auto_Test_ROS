# 固件升级压测

English: [README.md](README.md)

## 工具介绍

重复调用 Orbbec 驱动的 `firmware_update_tool`，按顺序循环使用一份或多份固件文件。
每轮测试只有同时满足以下条件才算通过：

```text
firmware_update_tool 退出码为 0
日志包含：Firmware tool completed successfully. Updated X/Y target device(s).
```

该工具不做固件版本校验、不做设备发现、不启动 launch，也不检查图像流。

每轮压测的典型流程：

```text
从固件列表取下一份固件 -> 调用 firmware_update_tool
终端输出同步保存到 logs/test_XXXX/update.log
检查退出码和成功日志 -> 继续下一轮
```

## 使用方法

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

### 多相机按 SN 批量升级

多相机时脚本会把多个 SN 合并成一次 `firmware_update_tool` 调用：

```bash
python3 ./firmware_update_stress_test/firmware_update_stress_test.py \
  --ros-version 2 \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --serial-number SN001,SN002,SN003 \
  --firmware /path/to/firmware_A.bin \
  --firmware /path/to/firmware_B.bin \
  --test-count 6
```

`--serial-number` 也可以重复传入：

```bash
--serial-number SN001 --serial-number SN002
```

## 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--firmware` | 必填 | 固件文件路径，可重复传入并按顺序循环 |
| `--test-count` | `10` | 升级命令调用次数；`0` = 持续运行到 `--duration` |
| `--duration` | `300` | `--test-count 0` 时使用，支持 `300`、`15m`、`2h` |
| `--restart-delay` | `2` | 两次升级命令之间的等待秒数 |
| `--serial-number` | 空 | 目标 SN，可重复或逗号分隔 |
| `--usb-port` | 空 | 单设备 USB port 选择器 |
| `--device-ip` | 空 | 单设备网络 IP 选择器 |
| `--device-port` | `8090` | 传给 firmware tool 的网络设备端口 |
| `--reconnect-timeout-sec` | `120` | 传给 `firmware_update_tool` |
| `--reconnect-poll-ms` | `1000` | 传给 `firmware_update_tool` |
| `--sdk-log-level` | `off` | 传给 `firmware_update_tool` |
| `--continue-on-error` | 关闭 | 传给 `firmware_update_tool` |

同一轮只能使用一种设备选择方式：SN、USB port 或 device IP。多相机场景使用 SN 批量模式。

## 结果文件

每次运行会创建结果目录：

```text
firmware_update_stress_test/results/YYYYMMDD_HHMMSS_firmware_update/
├── summary.md                  # 最终结果和每次压测通过/失败状态
├── result.json                 # 完整机器可读结果
└── logs/test_XXXX/update.log   # firmware_update_tool 终端输出
```
