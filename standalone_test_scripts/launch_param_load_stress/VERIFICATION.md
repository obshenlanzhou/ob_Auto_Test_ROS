# Verification Map

中文文档: [VERIFICATION.zh-CN.md](VERIFICATION.zh-CN.md)

This tool checks whether `config_file_path` YAML settings take effect in three
layers. Different parameters use different verification methods.

## 1. Parameter Check

Every top-level YAML key is compared against the launched driver parameter.

- ROS 2: `ros2 param dump /<camera_name>/<camera_name>` (bulk, falls back to per-param)
- ROS 1: `rosparam get /<camera_name>/<camera_name>` (bulk, falls back to per-param)

Booleans and numeric strings are normalized before comparing, so `true` and
`"true"` are treated as equal.

If the launch file can be resolved, YAML keys not declared by it are marked
`skipped`.

## 2. Topic Behavior Check

Stream-switch parameters are also verified by image topic behavior.

| YAML key | Topic |
| --- | --- |
| `enable_color` | `/<camera_name>/color/image_raw` |
| `enable_depth` | `/<camera_name>/depth/image_raw` |
| `enable_ir` | `/<camera_name>/ir/image_raw` |
| `enable_left_ir` | `/<camera_name>/left_ir/image_raw` |
| `enable_right_ir` | `/<camera_name>/right_ir/image_raw` |
| `enable_left_color` | `/<camera_name>/left_color/image_raw` |
| `enable_right_color` | `/<camera_name>/right_color/image_raw` |

Expected `true`: one image message must be received within `--topic-timeout`.

Expected `false`: no image message should be received during the disabled-stream
check window.

## 3. Getter Service Check

The following parameters are also read back from the device via read-only getter
services. The service check runs only when the service is advertised by the
current driver and device.

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

Placeholder values (`-1`, empty string, `ANY`, `none`, `null`) are skipped at
service level because the driver uses device defaults for those.

Services that are not advertised are reported as `unsupported` and do not fail
the run.

## 4. Result Meanings

| Status | Meaning |
| --- | --- |
| `passed` | Expected and observed values or behavior match. |
| `failed` | Parameter value, topic behavior, or advertised service readback does not match. |
| `skipped` | YAML key is not declared by the resolved launch file, or the service value is a placeholder. |
| `unsupported` | Getter service is not advertised by the current driver or device. |

Print this map directly from the script:

```bash
python3 ./launch_param_load_stress.py --show-verification-map
```
