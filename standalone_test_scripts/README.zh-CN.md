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
├── launch_restart_stream_check/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_restart_stream_check.py
│   └── results/                  # 运行时生成
├── launch_param_load_stress/
│   ├── README.md
│   ├── README.zh-CN.md
│   ├── launch_param_load_stress.py
│   ├── config/
│   └── results/                  # 运行时生成
```

各脚本目录下的 `results/` 是测试运行时生成的结果目录，用于保存日志和结果文件。

## 环境

脚本支持通过命令行参数加载 ROS 和相机驱动环境：

```bash
--ros-setup /opt/ros/humble/setup.bash
--driver-setup /path/to/orbbec_camera_ws/install/setup.bash
```

## 脚本索引

| 脚本目录 | 用途 | 说明 |
| --- | --- | --- |
| [launch_restart_stream_check](launch_restart_stream_check/README.zh-CN.md) | 反复重启 launch 并检查图像流恢复 | 适合重启出流稳定性压测 |
| [launch_param_load_stress](launch_param_load_stress/README.zh-CN.md) | 通过 `config_file_path` 压测 launch 参数加载 | 验证 ROS 参数、图像 topic 和 getter service，支持多相机和压测重复 |
| [export_load_stress_test](export_load_stress_test/README.zh-CN.md) | 交替导入/导出 JSON 并比较参数 | 适合参数导入导出一致性压测 |
| [preset_upgrade_stress_test](preset_upgrade_stress_test/README.zh-CN.md) | 交替升级 preset 并验证出流 | 适合 optional depth preset 升级压测 |

## 新增独立脚本规范

后续新增脚本时建议遵循：

```text
每个测试脚本放在独立目录中
脚本目录内放置 README.md 和 README.zh-CN.md
脚本名清晰表达测试场景
不要依赖 orbbec_camera_auto_test 框架模块
需要 ROS 时支持 --ros-version、--ros-setup、--driver-setup
最终结果写入该脚本专属 summary.md
测试通过返回 0，失败返回非 0
```
