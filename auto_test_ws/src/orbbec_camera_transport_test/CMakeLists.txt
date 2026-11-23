cmake_minimum_required(VERSION 3.16)
project(orbbec_camera_transport_test)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(sensor_msgs REQUIRED)

add_library(transport_subscriber_component SHARED src/transport_subscriber.cpp)
target_compile_features(transport_subscriber_component PRIVATE cxx_std_17)
if(COMMAND ament_target_dependencies)
  ament_target_dependencies(transport_subscriber_component rclcpp rclcpp_components sensor_msgs)
else()
  target_link_libraries(transport_subscriber_component rclcpp::rclcpp rclcpp_components::component
    sensor_msgs::sensor_msgs_library)
endif()
rclcpp_components_register_node(transport_subscriber_component
  PLUGIN "orbbec_camera_transport_test::TransportSubscriber"
  EXECUTABLE transport_subscriber)
install(TARGETS transport_subscriber_component LIBRARY DESTINATION lib)
install(DIRECTORY launch DESTINATION share/${PROJECT_NAME})
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/ros_distro.txt" "$ENV{ROS_DISTRO}\n")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/ros_distro.txt" DESTINATION share/${PROJECT_NAME})
ament_package()
