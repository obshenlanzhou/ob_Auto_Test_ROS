# Orbbec Camera Auto Test ROS

本仓库用于 Orbbec ROS 相机自动化测试，主要包含两类测试工具：

- `auto_test_ws/`：完整自动化测试工作区，包含功能测试、性能压测、launch 重启出流测试，以及本地 Web UI。
- `standalone_test_scripts/`：可单独交付和运行的独立测试脚本，不依赖 `orbbec_camera_auto_test` 自动化测试框架。

## 文档入口

- 完整自动化测试工作区文档：[auto_test_ws/README.md](auto_test_ws/README.md)
- 独立测试脚本文档：[standalone_test_scripts/README.zh-CN.md](standalone_test_scripts/README.zh-CN.md)
- Standalone scripts English guide：[standalone_test_scripts/README.md](standalone_test_scripts/README.md)

## 目录结构

```text
ob_Auto_Test_ROS/
├── README.md
├── auto_test_ws/
│   ├── README.md
│   ├── run_camera_auto_test.sh
│   ├── run_camera_auto_test_ui.sh
│   ├── src/
│   └── results/                  # 运行时生成
└── standalone_test_scripts/
    ├── README.md
    ├── README.zh-CN.md
    ├── export_load_stress_test/
    │   └── results/              # 运行时生成
    └── launch_restart_stream_check/
        └── results/              # 运行时生成
```

## 快速入口

进入完整自动化测试工作区：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/auto_test_ws
```

运行命令行测试：

```bash
./run_camera_auto_test.sh --help
```

启动本地 Web UI：

```bash
./run_camera_auto_test_ui.sh
```

浏览器访问：

```text
http://127.0.0.1:8000
```

运行独立测试脚本：

```bash
cd /home/slz/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts
python3 ./launch_restart_stream_check/launch_restart_stream_check.py --help
```

## 适用场景

- 需要完整测试流程、结果归档、Web UI 或 ROS package 方式运行时，使用 [auto_test_ws](auto_test_ws/README.md)。
- 需要把单个测试脚本交给客户、现场快速验证时，使用 [standalone_test_scripts](standalone_test_scripts/README.zh-CN.md)。
