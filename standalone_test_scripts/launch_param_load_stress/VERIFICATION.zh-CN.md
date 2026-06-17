# 验证映射说明

English: [VERIFICATION.md](VERIFICATION.md)

该工具通过三层逻辑判断 `config_file_path` YAML 设置是否生效。不同参数的额外验证方式不同。

## 1. 参数层验证

YAML 顶层的每个 key 都会先和启动后的驱动参数做比较。

- ROS2：`ros2 param get /<camera_name>/ob_camera_node <param>`
- ROS1：`rosparam get /<camera_name>/<camera_name>/<param>`

工具会在比较前统一 bool、数字字符串等类型，所以 `true` 和 `"true"` 会被视为相同。

如果脚本能解析到 launch 文件，YAML 中未在该 launch 声明的 key 会标记为
`skipped`。

## 2. Topic 行为验证

出流开关类参数会继续通过图像 topic 行为验证。

| YAML key | Topic |
| --- | --- |
| `enable_color` | `/<camera_name>/color/image_raw` |
| `enable_depth` | `/<camera_name>/depth/image_raw` |
| `enable_ir` | `/<camera_name>/ir/image_raw` |
| `enable_left_ir` | `/<camera_name>/left_ir/image_raw` |
| `enable_right_ir` | `/<camera_name>/right_ir/image_raw` |
| `enable_left_color` | `/<camera_name>/left_color/image_raw` |
| `enable_right_color` | `/<camera_name>/right_color/image_raw` |

期望值为 `true`：必须在 `--topic-timeout` 内收到一帧图像消息。

期望值为 `false`：禁用流短检查窗口内不应该收到图像消息。

## 3. Getter Service 验证

以下参数会在当前设备和驱动暴露对应只读 getter service 时，继续读取设备状态验证。

| YAML key | Getter service | ROS1 类型 | ROS2 类型 |
| --- | --- | --- | --- |
| `color_exposure` | `/<camera_name>/get_color_exposure` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `color_gain` | `/<camera_name>/get_color_gain` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `color_white_balance` | `/<camera_name>/get_white_balance` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `depth_exposure` | `/<camera_name>/get_depth_exposure` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `depth_gain` | `/<camera_name>/get_depth_gain` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `ir_exposure` | `/<camera_name>/get_ir_exposure` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `ir_gain` | `/<camera_name>/get_ir_gain` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `left_ir_exposure` | `/<camera_name>/get_left_ir_exposure` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `left_ir_gain` | `/<camera_name>/get_left_ir_gain` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `right_ir_exposure` | `/<camera_name>/get_right_ir_exposure` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `right_ir_gain` | `/<camera_name>/get_right_ir_gain` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |
| `enable_laser` | `/<camera_name>/get_laser_status` | `orbbec_camera/GetBool` | `orbbec_camera_msgs/srv/GetBool` |
| `enable_ldp` | `/<camera_name>/get_ldp_status` | `orbbec_camera/GetBool` | `orbbec_camera_msgs/srv/GetBool` |
| `enable_ptp_config` | `/<camera_name>/get_ptp_config` | `orbbec_camera/GetBool` | `orbbec_camera_msgs/srv/GetBool` |
| `point_cloud_decimation_filter_factor` | `/<camera_name>/get_point_cloud_decimation` | `orbbec_camera/GetInt32` | `orbbec_camera_msgs/srv/GetInt32` |

`-1`、空字符串、`ANY`、`none`、`null` 这类占位值在 service 层会被跳过，
因为驱动可能会使用设备默认值或自动选择值。

未暴露的 getter service 会记录为 `unsupported`，不会直接导致测试失败。

## 4. 结果含义

| 状态 | 含义 |
| --- | --- |
| `passed` | 期望值和实际参数、topic 行为或 service 读回值一致。 |
| `failed` | 参数值、topic 行为或已暴露 service 的读回值不一致。 |
| `skipped` | YAML key 未在解析到的 launch 中声明，或 service 期望值是占位值。 |
| `unsupported` | 当前驱动或设备没有暴露对应 getter service。 |

也可以直接通过脚本打印同样的映射：

```bash
python3 ./launch_param_load_stress.py --show-verification-map
```
