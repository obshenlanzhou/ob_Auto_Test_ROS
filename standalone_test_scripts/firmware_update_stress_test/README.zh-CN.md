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

### 多相机按 SN 批量升级

多相机时脚本会把多个 SN 合并成一次 `firmware_update_tool` 调用：

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

每个 `--camera` 使用逗号分隔的 `KEY=VALUE` 格式。支持 `name`、`serial-number`、
`usb-port`、`device-ip`、`device-port`、`config-file-path`，每个字段均可选。

## 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ros-version` | `$ROS_VERSION` 或 `2` | ROS 版本，可选 `1` 或 `2` |
| `--ros-setup` | `$ORBBEC_ROS_SETUP` 或空 | ROS 环境 setup 脚本路径 |
| `--driver-setup` | `$ORBBEC_CAMERA_SETUP` 或空 | Orbbec 驱动环境 setup 脚本路径 |
| `--firmware` | 必填 | 固件文件路径，可重复传入并按顺序循环 |
| `--run-count` | 空 | 升级命令最大调用次数 |
| `--continue-on-failure` | 关闭 | 记录失败并继续下一次升级；最终结果仍为失败 |
| `--duration` | 空 | 可选的最长运行时间，支持 `300`、`15m`、`2h` |
| `--restart-delay` | `2` | 两次升级命令之间的等待秒数 |
| `--camera` | 默认相机 | 相机目标配置，可重复传入 |
| `--reconnect-timeout-sec` | `120` | 传给 `firmware_update_tool` |
| `--reconnect-poll-ms` | `1000` | 传给 `firmware_update_tool` |
| `--sdk-log-level` | `debug` | 传给 `firmware_update_tool` |
| `--continue-on-error` | 关闭 | 仅传给 `firmware_update_tool`，不控制压测循环 |

`--run-count` 和 `--duration` 至少传入一个，也可以同时传入；同时传入时，任一条件先达到即结束。

同一相机配置中可组合兼容的选择字段。多个 SN 会作为一次 firmware tool 批量调用；
USB 和网络选择字段必须最终对应一个目标值。

## 结果文件

每次运行会创建结果目录：

```text
firmware_update_stress_test/results/YYYYMMDD_HHMMSS_firmware_update_v2.0.0/
├── summary.md                  # 最终结果和每次压测通过/失败状态
├── result.json                 # 完整机器可读结果
├── events.jsonl                # 结构化生命周期和进度事件
├── logs/test_XXXX/update.log   # firmware_update_tool 终端输出
└── logs/test_XXXX/sdk/Log/     # 每轮 firmware_update_tool SDK debug 日志
```
