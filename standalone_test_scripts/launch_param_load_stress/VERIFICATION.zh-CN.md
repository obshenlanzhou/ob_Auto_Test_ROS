# 验证映射说明

English: [VERIFICATION.md](VERIFICATION.md)

该工具通过三层逻辑判断 `config_file_path` YAML 设置是否生效。不同参数的验证方式不同。

## 1. 参数层验证

YAML 顶层的每个 key 都会和启动后的驱动参数做比较。

- ROS 2：`ros2 param dump /<camera_name>/<camera_name>`（批量查询，失败时退回逐个查询）
- ROS 1：`rosparam get /<camera_name>/<camera_name>`（批量查询，失败时退回逐个查询）

比较前会统一 bool 和数字字符串类型，`true` 和 `"true"` 视为相同。

如果脚本能解析到 launch 文件，YAML 中未在该 launch 声明的 key 会标记为 `skipped`。

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

以下参数会通过只读 getter service 读回设备真实状态来验证。仅在当前驱动和设备
暴露对应 service 时执行。

| YAML key | Getter service |
| --- | --- |
| `color_exposure` | `/<camera_name>/get_color_exposure` |
| `color_gain` | `/<camera_name>/get_color_gain` |
| `color_white_balance` | `/<camera_name>/get_white_balance` |
| `depth_exposure` | `/<camera_name>/get_depth_exposure` |
| `depth_gain` | `/<camera_name>/get_depth_gain` |
| `ir_exposure` | `/<camera_name>/get_ir_exposure` |
| `ir_gain` | `/<camera_name>/get_ir_gain` |
| `left_ir_exposure` | `/<camera_name>/get_left_ir_exposure` |
| `left_ir_gain` | `/<camera_name>/get_left_ir_gain` |
| `right_ir_exposure` | `/<camera_name>/get_right_ir_exposure` |
| `right_ir_gain` | `/<camera_name>/get_right_ir_gain` |
| `enable_laser` | `/<camera_name>/get_laser_status` |
| `enable_ldp` | `/<camera_name>/get_ldp_status` |
| `enable_ptp_config` | `/<camera_name>/get_ptp_config` |
| `point_cloud_decimation_filter_factor` | `/<camera_name>/get_point_cloud_decimation` |

`-1`、空字符串、`ANY`、`none`、`null` 等占位值在 service 层会被跳过，
驱动会使用设备默认值。

未暴露的 getter service 会记录为 `unsupported`，不会导致测试失败。

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
