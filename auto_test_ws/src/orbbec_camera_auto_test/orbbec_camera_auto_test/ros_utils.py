from __future__ import annotations

import importlib
import time
from typing import Any, Dict, Optional

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy


def resolve_message_type(type_name: str):
    if not type_name:
        raise ValueError("message type is required")
    package_name, _, message_name = type_name.split("/")
    module = importlib.import_module(f"{package_name}.msg")
    return getattr(module, message_name)


def resolve_service_type(type_name: str):
    package_name, _, service_name = type_name.split("/")
    module = importlib.import_module(f"{package_name}.srv")
    return getattr(module, service_name)


def make_qos_profile(name: str) -> QoSProfile | int:
    if name == "transient_local":
        return QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
    return 10


def deep_getattr(obj: Any, path: str, default: Any = None) -> Any:
    current = obj
    for part in path.split("."):
        if current is None or not hasattr(current, part):
            return default
        current = getattr(current, part)
    return current


def populate_message_fields(message: Any, values: Dict[str, Any]) -> Any:
    for key, value in values.items():
        current = getattr(message, key)
        if isinstance(value, dict) and not isinstance(current, (str, bytes)):
            populate_message_fields(current, value)
            setattr(message, key, current)
        else:
            setattr(message, key, value)
    return message


class RosHarness:
    def __init__(self, node_name: str):
        self.node_name = node_name
        self.node: Optional[Node] = None

    def __enter__(self) -> "RosHarness":
        if not rclpy.ok():
            rclpy.init()
        self.node = Node(self.node_name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.node is not None:
            self.node.destroy_node()
            self.node = None
        if rclpy.ok():
            rclpy.shutdown()

    def spin_once(self, timeout: float = 0.1) -> None:
        rclpy.spin_once(self.node, timeout_sec=timeout)

    def spin_until(self, predicate, timeout: float, description: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.spin_once(0.1)
        raise TimeoutError(f"Timed out waiting for {description}")

    def wait_for_node(self, node_name: str, namespace: str = "/", timeout: float = 30.0) -> None:
        namespace = "/" if namespace == "/" else f"/{namespace.strip('/')}"

        def predicate() -> bool:
            return any(
                current_name == node_name and current_ns == namespace
                for current_name, current_ns in self.node.get_node_names_and_namespaces()
            )

        self.spin_until(predicate, timeout, f"node {namespace}/{node_name}")

    def wait_for_topic(
        self, topic_name: str, topic_type: Optional[str] = None, timeout: float = 30.0
    ) -> None:
        def predicate() -> bool:
            for current_name, current_types in self.node.get_topic_names_and_types():
                if current_name != topic_name:
                    continue
                if not topic_type:
                    return True
                if topic_type in current_types:
                    return True
            return False

        description = f"topic {topic_name}"
        if topic_type:
            description += f" ({topic_type})"
        self.spin_until(predicate, timeout, description)

    def wait_for_service(self, service_name: str, srv_type, timeout: float = 30.0) -> None:
        client = self.node.create_client(srv_type, service_name)
        try:
            self.spin_until(
                lambda: client.wait_for_service(timeout_sec=0.1), timeout, f"service {service_name}"
            )
        finally:
            self.node.destroy_client(client)

    def wait_for_publishers(self, topic_name: str, timeout: float = 30.0) -> None:
        self.spin_until(
            lambda: len(self.node.get_publishers_info_by_topic(topic_name)) > 0,
            timeout,
            f"publisher on {topic_name}",
        )

    def wait_for_message(
        self,
        topic_name: str,
        msg_type,
        timeout: float = 30.0,
        qos_profile: QoSProfile | int = 10,
    ):
        received = {"message": None}

        def callback(message):
            received["message"] = message

        subscription = self.node.create_subscription(msg_type, topic_name, callback, qos_profile)
        try:
            self.spin_until(
                lambda: received["message"] is not None, timeout, f"message on {topic_name}"
            )
            return received["message"]
        finally:
            self.node.destroy_subscription(subscription)

    def call_service(
        self,
        service_name: str,
        srv_type,
        request_data: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ):
        client = self.node.create_client(srv_type, service_name)
        try:
            self.spin_until(
                lambda: client.wait_for_service(timeout_sec=0.1), timeout, f"service {service_name}"
            )
            request = srv_type.Request()
            if request_data:
                populate_message_fields(request, request_data)
            future = client.call_async(request)
            self.spin_until(lambda: future.done(), timeout, f"response from {service_name}")
            return future.result()
        finally:
            self.node.destroy_client(client)

    def graph_snapshot(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {"name": name, "namespace": namespace}
                for name, namespace in self.node.get_node_names_and_namespaces()
            ],
            "topics": [
                {"name": name, "types": types}
                for name, types in self.node.get_topic_names_and_types()
            ],
            "services": [
                {"name": name, "types": types}
                for name, types in self.node.get_service_names_and_types()
            ],
        }
