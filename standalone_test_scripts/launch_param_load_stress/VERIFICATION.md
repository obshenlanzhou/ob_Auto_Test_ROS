# Verification Map

中文文档: [VERIFICATION.zh-CN.md](VERIFICATION.zh-CN.md)

This tool checks whether `config_file_path` YAML settings take effect in three
layers. Different parameters use different extra checks.

## 1. Parameter Check

Every top-level YAML key is checked against the launched driver parameter.

- ROS2: `ros2 param get /<camera_name>/ob_camera_node <param>`
- ROS1: `rosparam get /<camera_name>/<camera_name>/<param>`

The tool normalizes booleans and numeric strings before comparing, so `true`
and `"true"` are treated as the same value.

If the launch file can be resolved, YAML keys that are not declared by the
launch file are marked `skipped`.

## 2. Topic Behavior Check

Stream switch parameters are also checked by image topic behavior.

| YAML key | Topic |
| --- | --- |
| `enable_color` | `/<camera_name>/color/image_raw` |
| `enable_depth` | `/<camera_name>/depth/image_raw` |
| `enable_ir` | `/<camera_name>/ir/image_raw` |
| `enable_left_ir` | `/<camera_name>/left_ir/image_raw` |
| `enable_right_ir` | `/<camera_name>/right_ir/image_raw` |
| `enable_left_color` | `/<camera_name>/left_color/image_raw` |
| `enable_right_color` | `/<camera_name>/right_color/image_raw` |

Expected `true`: one image message must be received before `--topic-timeout`.

Expected `false`: no image message should be received during the disabled-stream
check.

## 3. Getter Service Check

The following parameters are also checked by read-only getter services when the
current device and driver advertise them.

| YAML key | Getter service | ROS1 type | ROS2 type |
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

Placeholder values such as `-1`, empty strings, `ANY`, `none`, or `null` are
skipped at service level because the driver may use device defaults or automatic
selection.

Getter services that are not advertised are reported as `unsupported` and do
not fail the run.

## 4. Result Meanings

| Status | Meaning |
| --- | --- |
| `passed` | Expected and observed values or behavior match. |
| `failed` | Parameter value, topic behavior, or advertised service readback does not match. |
| `skipped` | YAML key is not declared by the resolved launch file, or service value is a placeholder. |
| `unsupported` | Getter service is not advertised by the current driver/device. |

You can print the same map from the script:

```bash
python3 ./launch_param_load_stress.py --show-verification-map
```
