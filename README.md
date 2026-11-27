# Orbbec Camera Auto Test ROS

本仓库用于 Orbbec ROS 相机自动化测试，主要包含两类测试工具：

- `auto_test_ws/`：完整自动化测试工作区，包含功能测试、性能压测、launch 重启出流测试，以及本地 Web UI。
- `standalone_test_scripts/`：可单独交付和运行的独立测试脚本，不依赖 `orbbec_camera_auto_test` 自动化测试框架。

## 仓库地址

- [GitHub 源仓库](https://github.com/obshenlanzhou/ob_Auto_Test_ROS)
- [Gitee 镜像](https://gitee.com/kong-yue1/ob_Auto_Test_ROS)

通过 HTTPS 克隆 Gitee 镜像。

```bash
git clone https://gitee.com/kong-yue1/ob_Auto_Test_ROS.git
```

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
    ├── firmware_update_stress_test/
    │   └── results/              # 运行时生成
    ├── image_receive_stats_test/
    ├── launch_param_load_stress/
    │   └── results/              # 运行时生成
    ├── launch_restart_stream_check/
    │   └── results/              # 运行时生成
    └── preset_upgrade_stress_test/
        └── results/              # 运行时生成
```

## 快速入口

进入完整自动化测试工作区：

```bash
cd "$HOME/ORBBEC/ob_Auto_Test_ROS/auto_test_ws"
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

页面顶部可在“自动化框架”和“独立脚本”之间切换；独立脚本入口也可直接访问
`http://127.0.0.1:8000/?workspace=standalone`。两类任务共用实时日志和历史结果。

运行独立测试脚本：

```bash
cd "$HOME/ORBBEC/ob_Auto_Test_ROS/standalone_test_scripts"
python3 ./launch_restart_stream_check/launch_restart_stream_check.py --help
```

更多独立脚本入口见 [standalone_test_scripts/README.zh-CN.md](standalone_test_scripts/README.zh-CN.md)。

## Web UI 在线更新

通过 Git 克隆仓库后，在本机 Web UI 顶部打开「在线更新」，点击「检查更新」查看当前
commit 和远端最新 commit。点击「更新并重启」后，工具会快进到该 commit，重启 Web UI
并自动刷新页面。

在线更新跟踪当前 Git 分支配置的上游。直接从 GitHub 克隆时默认检查 GitHub，从 Gitee
镜像克隆时默认检查 Gitee。更新需要工作区干净、分支可快进，以及当前测试和结果打包已结束。

## 适用场景

- 需要完整测试流程、结果归档、Web UI 或 ROS package 方式运行时，使用 [auto_test_ws](auto_test_ws/README.md)。
- 需要把单个测试脚本交给客户、现场快速验证时，使用 [standalone_test_scripts](standalone_test_scripts/README.zh-CN.md)。
