# launch 参数加载压测

English: [README.md](README.md)

## 工具介绍

通过 `config_file_path` 向 Orbbec ROS 相机驱动传入 YAML 配置文件，验证每项设置
是否生效。支持多次重复压测和多相机场景。

每次 launch 周期从三个层面验证：

```text
ROS 参数       — 批量查询并与配置 YAML 对比
图像 topic     — 出流开关类参数（enable_color、enable_depth 等）
                通过是否收到图像消息来验证
Getter service — 曝光、增益、白平衡、激光、LDP、PTP、点云降采样
                通过 service 读回设备真实状态验证
```

## 使用方法

### 单相机

```bash
cd standalone_test_scripts

python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --ros-setup /opt/ros/humble/setup.bash \
  --driver-setup /path/to/camera_ws/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --config-file-path ./config/sample_config_file_path.yaml
```

ROS 1：

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 1 \
  --ros-setup /opt/ros/noetic/setup.bash \
  --driver-setup /path/to/camera_ws/devel/setup.bash \
  --launch-file gemini_330_series.launch \
  --config-file-path ./config/sample_config_file_path.yaml
```

### 多相机与压测

多次传入 `--camera` 指定每台设备，用 `--repeat N` 连续运行多轮：

```bash
python3 ./launch_param_load_stress/launch_param_load_stress.py \
  --ros-version 2 \
  --driver-setup /path/to/install/setup.bash \
  --launch-file gemini_330_series.launch.py \
  --camera camera1,usb_port=2-1,config_file_path=./config/cam1.yaml \
  --camera camera2,usb_port=2-2,config_file_path=./config/cam2.yaml \
  --repeat 10
```

`--camera` 格式：`name[,serial_number=SN][,usb_port=X][,config_file_path=/path]`

也可以用 `--config-file-path` 指定公用配置，作为未配置 `config_file_path` 的相机
的默认值。

### 可配置参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--repeat N` | `1` | 完整启动→检查→停止的循环次数 |
| `--startup-timeout SECS` | `30` | 等待设备初始化完成的最大秒数 |
| `--topic-timeout SECS` | `20` | 等待每个已启用流 topic 的最大秒数 |
| `--service-timeout SECS` | `15` | 每次参数/service 查询的最大秒数 |
| `--save-images-count N` | `0` | 每台相机每个已启用流保存的图片数（`0` = 不存图） |
| `--jpg-quality Q` | `80` | 保存图片的 JPEG 压缩质量（1–100） |
| `--skip-topic-check` | — | 跳过图像 topic 验证 |
| `--skip-service-check` | — | 跳过 getter service 验证 |

### 配置文件

配置 YAML 指定需要加载并验证的参数值。内置示例文件：

```text
launch_param_load_stress/config/sample_config_file_path.yaml
```

复制并修改后传入：

```bash
cp ./config/sample_config_file_path.yaml /tmp/my_config.yaml
# 按需修改值后通过 --config-file-path 传入
```

占位值（`-1`、空字符串、`ANY`、`none`、`null`）在 service 检查层会被跳过，
驱动会使用设备默认值。

## 结果文件

每次运行会创建结果目录：

```text
launch_param_load_stress/results/YYYYMMDD_HHMMSS_launch_param_load_stress/
├── run001/
│   ├── camera1.log        # camera1 的 launch 日志
│   └── camera2.log        # camera2 的 launch 日志（多相机时）
├── run002/
│   └── ...
├── images/                # 仅在 --save-images-count > 0 时生成
│   ├── run001/
│   │   └── camera1/<topic>/image_0001.jpg
│   └── run002/
│       └── ...
├── summary.md             # 每轮通过/失败汇总
└── result.json            # 所有轮次的机器可读结果
```
