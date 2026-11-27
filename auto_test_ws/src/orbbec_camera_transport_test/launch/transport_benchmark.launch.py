# Copyright 2026 Orbbec Inc.
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import yaml
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    GroupAction,
    IncludeLaunchDescription,
    OpaqueFunction,
    SetLaunchConfiguration,
)
from launch.events import Shutdown
from launch.launch_description_sources import (
    PythonLaunchDescriptionSource,
    load_python_launch_file_as_module,
)
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import ComposableNodeContainer, LoadComposableNodes, Node
from launch_ros.descriptions import ComposableNode

PACKAGE = 'orbbec_camera_transport_test'
PLUGIN = 'orbbec_camera_transport_test::TransportSubscriber'
CONTAINER = '/transport_benchmark_container'
STREAMS = {
    'enable_color': 'color/image_raw',
    'enable_depth': 'depth/image_raw',
    'enable_ir': 'ir/image_raw',
    'enable_left_ir': 'left_ir/image_raw',
    'enable_right_ir': 'right_ir/image_raw',
    'enable_left_color': 'left_color/image_raw',
    'enable_right_color': 'right_color/image_raw',
    'enable_point_cloud': 'depth/points',
    'enable_colored_point_cloud': 'depth_registered/points',
}


def launch_value(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def has_static_container(actions):
    for action in actions:
        if isinstance(action, ComposableNodeContainer):
            return True
        if isinstance(action, GroupAction) and has_static_container(action.get_sub_entities()):
            return True
    return False


def load_legacy_camera(context, load_parameters, declarations, camera_name):
    if callable(load_parameters):
        parameters = [load_parameters(context, declarations)]
    else:
        parameters = [{arg.name: LaunchConfiguration(arg.name)} for arg in declarations]
    return [LoadComposableNodes(
        target_container=CONTAINER,
        composable_node_descriptions=[ComposableNode(
            package='orbbec_camera',
            plugin='orbbec_camera::OBCameraNodeDriver',
            name=camera_name,
            namespace=camera_name,
            parameters=parameters,
            extra_arguments=[{'use_intra_process_comms': True}],
        )],
    )]


def setup_configured_benchmark(config, path):
    streams = config["streams"]
    backend = config["gpu_backend"]
    composed = config["transport_mode"] == "intra_process" or backend.startswith("NITROS")
    topics = [stream["topic"] for stream in streams]
    parameters = [{
        "topics": topics,
        "required_topics": topics,
        **{key: [stream[field] for stream in streams] for key, field in (
            ("camera_names", "camera_name"), ("stream_names", "stream"),
            ("stream_kinds", "kind"), ("gpu_topics", "gpu_topic"), ("host_clocks", "host_clock"),
        )},
        **{key: config[key] for key in ("transport_mode", "transport_reliability", "duration_seconds",
                                        "warmup_frames", "warmup_timeout_sec", "gpu_backend")},
        "results_dir": str(path.parent),
    }]
    drivers = []
    for launch in config["launches"]:
        arguments = dict(launch["arguments"])
        if composed:
            module = load_python_launch_file_as_module(launch["path"])
            declared = {action.name for action in module.generate_launch_description().entities
                        if isinstance(action, DeclareLaunchArgument)}
            if not {"component_container_name", "use_intra_process_comms"} <= declared:
                raise RuntimeError(f"{launch['path']} must pass the shared container and IPC settings "
                                   "to its cameras; specify individual camera launches instead")
            arguments["component_container_name"] = CONTAINER
        drivers.append(GroupAction([IncludeLaunchDescription(
            PythonLaunchDescriptionSource(launch["path"]), launch_arguments=arguments.items(),
        )]))
    shutdown = [EmitEvent(event=Shutdown(reason="Transport benchmark exited"))]
    if composed:
        probe = ComposableNodeContainer(
            package="rclcpp_components",
            executable="component_container_mt",
            name=CONTAINER.lstrip("/"),
            namespace="",
            output="screen",
            on_exit=shutdown,
            composable_node_descriptions=[ComposableNode(
                package=PACKAGE, plugin=PLUGIN, parameters=parameters,
            )],
        )
    else:
        probe = Node(package=PACKAGE, executable="transport_subscriber", parameters=parameters,
                     output="screen", on_exit=shutdown)
    # Start the test receiver before loading the cameras so it can see each stream's warmup.
    return [probe, *drivers]


def setup_benchmark(context):
    path = Path(LaunchConfiguration('benchmark_config').perform(context))
    config = json.loads(path.read_text(encoding='utf-8'))
    if "launches" in config:
        return setup_configured_benchmark(config, path)
    camera_path = config['camera_launch']
    arguments = dict(config['camera_arguments'])
    intra_process = config['transport_mode'] == 'intra_process'

    module = load_python_launch_file_as_module(camera_path)
    entities = module.generate_launch_description().entities
    declarations = [
        action for action in entities
        if isinstance(action, DeclareLaunchArgument)
    ]
    declared = {action.name for action in declarations}
    if 'time_domain' in declared:
        arguments.setdefault('time_domain', 'global')
    if 'enable_gpu_transport' in declared:
        arguments.setdefault('enable_gpu_transport', False)
    if 'use_intra_process_comms' in declared:
        arguments['use_intra_process_comms'] = intra_process

    effective = dict(arguments)
    config_file = arguments.get('config_file_path')
    if config_file:
        config_path = Path(config_file).expanduser()
        if not config_path.is_absolute() and not config_path.is_file():
            config_path = Path(camera_path).parent.parent / 'config' / config_path
        config_path = config_path.resolve(strict=True)
        arguments['config_file_path'] = str(config_path)
        with config_path.open(encoding='utf-8') as file:
            camera_overrides = yaml.safe_load(file)
        if isinstance(camera_overrides, dict):
            for name in ('serial_number', 'usb_port'):
                if arguments.get(name) and name in camera_overrides:
                    if str(arguments[name]) != str(camera_overrides[name]):
                        raise RuntimeError(f'Camera config overrides the requested {name}')
            effective.update(camera_overrides)
    camera_name = str(effective.get('camera_name') or 'camera').strip('/')
    topics = []
    required_topics = []
    for flag, suffix in STREAMS.items():
        setting = launch_value(effective.get(flag, ''))
        topic = f'/{camera_name}/{suffix}'
        if setting != 'false':
            topics.append(topic)
        if setting == 'true':
            required_topics.append(topic)

    parameters = [{
        'topics': topics,
        **({'required_topics': required_topics} if required_topics else {}),
        'transport_mode': config['transport_mode'],
        'transport_reliability': config.get('transport_reliability', 'best_effort'),
        'duration_seconds': float(config['duration_seconds']),
        **{key: config[key] for key in ('warmup_frames', 'warmup_timeout_sec') if key in config},
        'results_dir': str(path.parent),
    }]
    shutdown = [EmitEvent(event=Shutdown(reason='Transport benchmark exited'))]
    driver = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(camera_path),
        launch_arguments={name: launch_value(value) for name, value in arguments.items()}.items(),
    )
    if not intra_process:
        return [driver, Node(
            package=PACKAGE,
            executable='transport_subscriber',
            parameters=parameters,
            output='screen',
            on_exit=shutdown,
        )]

    container = ComposableNodeContainer(
        package='rclcpp_components',
        executable='component_container',
        name=CONTAINER.lstrip('/'),
        namespace='',
        output='screen',
        on_exit=shutdown,
        composable_node_descriptions=[ComposableNode(
            package=PACKAGE,
            plugin=PLUGIN,
            parameters=parameters,
            extra_arguments=[{'use_intra_process_comms': True}],
        )],
    )
    if {'component_container_name', 'use_intra_process_comms'} <= declared:
        arguments['component_container_name'] = CONTAINER
        return [container, IncludeLaunchDescription(
            PythonLaunchDescriptionSource(camera_path),
            launch_arguments={name: launch_value(value) for name, value in arguments.items()}.items(),
        )]

    # Older Orbbec launches use a parameter loader or forward declared arguments
    # to a static camera component. Reuse those parameters in the test container.
    load_parameters = getattr(module, 'load_parameters', None)
    if 'camera_name' not in declared or not (callable(load_parameters) or has_static_container(entities)):
        raise RuntimeError('The selected launch cannot load an Orbbec camera with IPC enabled')
    return [container, GroupAction([
        *(SetLaunchConfiguration(name, launch_value(value)) for name, value in arguments.items()),
        *declarations,
        OpaqueFunction(function=load_legacy_camera,
                       args=[load_parameters, declarations, camera_name]),
    ])]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('benchmark_config'),
        OpaqueFunction(function=setup_benchmark),
    ])
