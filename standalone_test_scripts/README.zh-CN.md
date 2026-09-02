# 独立测试脚本

English: [README.md](README.md)

该目录用于存放可单独交付给客户使用的测试脚本。这些脚本不依赖
`orbbec_camera_auto_test` 自动化测试框架，尽量只依赖 ROS、Orbbec 相机驱动工作空间
以及 Python 标准库。

## 目录结构

```text
standalone_test_scripts/
├── README.md
├── README.zh-CN.md
├── export_load_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── export_load_stress_test.py
│   ├── config/
│   └── results/                  # 运行时生成
├── preset_upgrade_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── preset_upgrade_stress_test.py
│   ├── config/
│   └── results/                  # 运行时生成
├── firmware_update_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── firmware_update_stress_test.py
│   └── results/                  # 运行时生成
├── launch_restart_stream_check/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_restart_stream_check.py
│   └── results/                  # 运行时生成
├── stream_toggle_stress_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── stream_toggle_stress_test.py
│   └── results/                  # 运行时生成
├── launch_param_load_stress/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_param_load_stress.py
│   ├── config/
│   └── results/                  # 运行时生成
├── image_receive_stats_test/
│   ├── README.md
│   ├── README.zh-CN.md
│   └── image_topic_receive_stats.py
```

多数压测脚本目录下的 `results/` 是测试运行时生成的结果目录，用于保存日志和结果文件。`image_receive_stats_test` 按启动参数写入指定输出目录。

## 精简打包压测结果

压测结束后，可以将一个独立压测结果目录打包为精简的 `.tar.gz`：

```bash
python3 ./package_stress_results.py \
  ./stream_toggle_stress_test/results/20260824_150213_stream_toggle_v1.9.7
```

默认在结果目录同级生成 `<结果目录名>.tar.gz`。指定输出路径：

```bash
python3 ./package_stress_results.py /path/to/result \
  --output /path/to/camera_stress_result.tar.gz
```

## 环境

多数会启动相机驱动的脚本支持通过命令行参数加载 ROS 和相机驱动环境：

```bash
--ros-setup /opt/ros/humble/setup.bash
--driver-setup /path/to/orbbec_camera_ws/install/setup.bash
```

`image_receive_stats_test` 是订阅端监测工具，但同样支持上述环境参数，方便统一启动。

## 通用参数与结果契约

所有公开长参数统一使用 kebab-case。循环上限统一使用 `--run-count`，运行时间上限统一
使用 `--duration`；两者同时设置时，任一上限先达到即结束。相机使用可重复的 launch
参数风格配置：

```bash
--camera name=camera_01,serial-number=SN001,usb-port=2-1
```

支持字段为 `name`、`serial-number`、`usb-port`、`device-ip`、`device-port` 和
`config-file-path`。每个字段都可以填写或不填，兼容的字段可以组合。无需显式相机配置
的脚本会使用自己的默认值。

所有脚本都支持 `--continue-on-failure`。该参数默认关闭，因此首次失败即停止测试；
开启后会记录失败，并按脚本类型继续下一轮、下一个 Preset、下一次升级或后续帧，
但最终结果和退出码仍会报告失败。

每次运行都会生成 `terminal.log`，实时保存脚本自身的标准输出和标准错误，同时保持终端正常输出。
未指定 `--results-dir` 时，默认结果目录名采用
`YYYYMMDD_HHMMSS_<测试名称>_v<工具版本>` 格式。
每次完成的运行还会生成 `result.json`、`summary.md` 和 `events.jsonl`。
`result.json` 的 `invocation` 字段记录可复现的完整命令字符串、原始参数数组和运行工作目录。
`result.json` 的状态统一为 `passed`、`failed`、`interrupted`，对应退出码分别为
`0`、`1`、`130`；命令行参数错误返回 `2`。脚本特有的日志、图片、CSV 和导出文件
统一在 `result.json` 的 `artifacts` 中列出。

## 本地 Web UI 集成

每个脚本目录包含一份由开发者维护的 `ui_manifest.json`。本地 Web UI 会发现这些清单，
自动生成基础/高级结构化表单，不提供原始参数输入框：

```text
http://127.0.0.1:8000/?workspace=standalone
```

UI 运行目录位于 `auto_test_ws/results/ui_runs/`，目录名采用
`YYYYMMDD_HHMMSS_standalone_<测试 ID>_v<工具版本>` 格式。

清单声明字段类型、默认值、风险等级和停止策略。脚本仍须保持独立：清单可以描述脚本的
CLI，但脚本本身不能导入 Web UI 包。

## 脚本索引

| 脚本目录 | 用途 | 说明 |
| --- | --- | --- |
| [launch_restart_stream_check](launch_restart_stream_check/README.zh-CN.md) | 反复重启 launch 并检查出流恢复 | 每次重启保存图像、点云和 IMU 证据 |
| [stream_toggle_stress_test](stream_toggle_stress_test/README.zh-CN.md) | 逐路或整体开关图像流并验证恢复 | on 阶段同时验证点云和 IMU，支持 ROS1/ROS2 |
| [launch_param_load_stress](launch_param_load_stress/README.zh-CN.md) | 通过 `config_file_path` 压测 launch 参数加载 | 验证参数、服务、图像、点云和 IMU |
| [export_load_stress_test](export_load_stress_test/README.zh-CN.md) | 交替导入/导出 JSON 并比较参数 | 同时固定并验证点云/IMU 基线 |
| [preset_upgrade_stress_test](preset_upgrade_stress_test/README.zh-CN.md) | 交替升级 preset 并验证出流 | 每次 preset 测试保存图像、点云和 IMU 证据 |
| [firmware_update_stress_test](firmware_update_stress_test/README.zh-CN.md) | 反复调用 `firmware_update_tool --firmware_path` 并检查成功日志 | 适合固件升级命令压测，支持按 SN 批量升级 |
| [image_receive_stats_test](image_receive_stats_test/README.zh-CN.md) | 订阅图像话题并统计接收间隔 | 适合 ROS1/ROS2 订阅端停流、卡顿和时间戳监测 |

## 新增独立脚本规范

后续新增脚本时建议遵循：

```text
每个测试脚本放在独立目录中
脚本目录内放置 README.md 和 README.zh-CN.md
需要出现在本地 Web UI 时提供 ui_manifest.json
脚本名清晰表达测试场景
不要依赖 orbbec_camera_auto_test 框架模块
需要 ROS 时支持 --ros-version、--ros-setup、--driver-setup
适用时支持统一的相机、生命周期和环境参数
支持 --continue-on-failure，且默认关闭
按统一契约写入 result.json、summary.md 和 events.jsonl
通过返回 0，失败返回 1，中断返回 130，参数错误返回 2
```
