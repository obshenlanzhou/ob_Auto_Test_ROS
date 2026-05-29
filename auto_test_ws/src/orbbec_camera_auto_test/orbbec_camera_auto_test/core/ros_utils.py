from __future__ import annotations

import importlib
import time
from typing import Any, Dict, Optional

def _split_ros_type(type_name: str) -> tuple[str, str]:
    if not type_name:
        raise ValueError("ROS type is required")
    parts = type_name.split("/")
    if len(parts) == 3:
        return parts[0], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"Unsupported ROS type format: {type_name}")


def _ros1_package_name(package_name: str) -> str:
    if package_name == "orbbec_camera_msgs":
        return "orbbec_camera"
    return package_name


def resolve_topic_name(topic_name: str, ros_version: str = "2") -> str:
    if str(ros_version) != "1":
        return topic_name
    if topic_name.endswith("/depth_filter_status"):
        return topic_name[: -len("/depth_filter_status")] + "/filter_status"
    return topic_name


def resolve_message_type(type_name: str, ros_version: str = "2"):
    package_name, message_name = _split_ros_type(type_name)
    if str(ros_version) == "1":
        package_name = _ros1_package_name(package_name)
    module = importlib.import_module(f"{package_name}.msg")
    return getattr(module, message_name)


def resolve_service_type(type_name: str, ros_version: str = "2"):
    package_name, service_name = _split_ros_type(type_name)
    if str(ros_version) == "1":
        package_name = _ros1_package_name(package_name)
    module = importlib.import_module(f"{package_name}.srv")
    return getattr(module, service_name)


def make_qos_profile(name: str, ros_version: str = "2"):
    if str(ros_version) == "1":
        return 10
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

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
    def __init__(self, node_name: str, ros_version: str = "2"):
        self.node_name = node_name
        self.ros_version = str(ros_version)
        self.node = None
        self._rclpy = None
        self._rospy = None

    def __enter__(self) -> "RosHarness":
        if self.ros_version == "1":
            import rospy

            self._rospy = rospy
            if not rospy.core.is_initialized():
                rospy.init_node(self.node_name, anonymous=True, disable_signals=True)
            self.node = _Ros1NodeAdapter(rospy)
            return self

        import rclpy
        from rclpy.node import Node

        self._rclpy = rclpy
        if not rclpy.ok():
            rclpy.init()
        self.node = Node(self.node_name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ros_version == "1":
            if self.node is not None:
                self.node.close()
                self.node = None
            return

        if self.node is not None:
            self.node.destroy_node()
            self.node = None
        if self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()

    def spin_once(self, timeout: float = 0.1) -> None:
        if self.ros_version == "1":
            time.sleep(timeout)
            return
        self._rclpy.spin_once(self.node, timeout_sec=timeout)

    def spin_until(self, predicate, timeout: float, description: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.spin_once(0.1)
        raise TimeoutError(f"Timed out waiting for {description}")

    def wait_for_node(self, node_name: str, namespace: str = "/", timeout: float = 30.0) -> None:
        namespace = "/" if namespace == "/" else f"/{namespace.strip('/')}"

        if self.ros_version == "1":
            import rosnode

            expected = f"{namespace}/{node_name}".replace("//", "/")

            def ros1_predicate() -> bool:
                try:
                    return expected in rosnode.get_node_names()
                except Exception:
                    return False

            self.spin_until(ros1_predicate, timeout, f"node {expected}")
            return

        def predicate() -> bool:
            return any(
                current_name == node_name and current_ns == namespace
                for current_name, current_ns in self.node.get_node_names_and_namespaces()
            )

        self.spin_until(predicate, timeout, f"node {namespace}/{node_name}")

    def wait_for_topic(
        self, topic_name: str, topic_type: Optional[str] = None, timeout: float = 30.0
    ) -> None:
        topic_name = resolve_topic_name(topic_name, self.ros_version)
        if self.ros_version == "1":
            def ros1_predicate() -> bool:
                try:
                    for current_name, current_type in self._rospy.get_published_topics():
                        if current_name != topic_name:
                            continue
                        if not topic_type:
                            return True
                        pkg, msg = _split_ros_type(topic_type)
                        return current_type == f"{_ros1_package_name(pkg)}/{msg}"
                except Exception:
                    return False
                return False

            self.spin_until(ros1_predicate, timeout, f"topic {topic_name}")
            return

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
        if self.ros_version == "1":
            self._rospy.wait_for_service(service_name, timeout=timeout)
            return

        client = self.node.create_client(srv_type, service_name)
        try:
            self.spin_until(
                lambda: client.wait_for_service(timeout_sec=0.1), timeout, f"service {service_name}"
            )
        finally:
            self.node.destroy_client(client)

    def wait_for_publishers(self, topic_name: str, timeout: float = 30.0) -> None:
        topic_name = resolve_topic_name(topic_name, self.ros_version)
        if self.ros_version == "1":
            self.wait_for_topic(topic_name, timeout=timeout)
            return
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
        qos_profile: Any = 10,
    ):
        topic_name = resolve_topic_name(topic_name, self.ros_version)
        if self.ros_version == "1":
            del qos_profile
            return self._rospy.wait_for_message(topic_name, msg_type, timeout=timeout)

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
        if self.ros_version == "1":
            self._rospy.wait_for_service(service_name, timeout=timeout)
            proxy = self._rospy.ServiceProxy(service_name, srv_type)
            request = srv_type._request_class()
            if request_data:
                populate_message_fields(request, request_data)
            return proxy(request)

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
        if self.ros_version == "1":
            import rosnode
            import rosservice

            return {
                "nodes": [{"name": name, "namespace": ""} for name in rosnode.get_node_names()],
                "topics": [
                    {"name": name, "types": [type_name]}
                    for name, type_name in self._rospy.get_published_topics()
                ],
                "services": [
                    {"name": name, "types": []}
                    for name in rosservice.get_service_list()
                ],
            }
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


class _Ros1NodeAdapter:
    def __init__(self, rospy, ros_version: str = "1") -> None:
        self.rospy = rospy
        self.ros_version = ros_version
        self._timers = []

    def create_subscription(self, msg_type, topic_name, callback, qos_profile=10):
        del qos_profile
        topic_name = resolve_topic_name(topic_name, self.ros_version)
        return self.rospy.Subscriber(topic_name, msg_type, callback, queue_size=10)

    def destroy_subscription(self, subscription) -> None:
        subscription.unregister()

    def create_timer(self, period_seconds: float, callback):
        timer = self.rospy.Timer(
            self.rospy.Duration(period_seconds),
            lambda event: callback(),
        )
        self._timers.append(timer)
        return timer

    def destroy_timer(self, timer) -> None:
        timer.shutdown()
        if timer in self._timers:
            self._timers.remove(timer)

    def close(self) -> None:
        for timer in list(self._timers):
            timer.shutdown()
        self._timers = []
