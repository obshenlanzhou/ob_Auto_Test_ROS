// Copyright 2026 Orbbec Inc.
// SPDX-License-Identifier: Apache-2.0

#pragma once

#if defined(TRANSPORT_CUDA_BUFFER) || defined(TRANSPORT_NITROS_BUFFER) || \
    defined(TRANSPORT_NITROS_GXF)
#include <cuda_runtime_api.h>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>

#if defined(TRANSPORT_CUDA_BUFFER)
#include <cuda_buffer/cuda_buffer_api.hpp>
#else
#include <isaac_ros_nitros_image_type/nitros_image.hpp>
#include <isaac_ros_nitros_point_cloud_type/nitros_point_cloud.hpp>
#if defined(TRANSPORT_NITROS_GXF)
#include <gxf/core/entity.hpp>
#include <gxf/multimedia/video.hpp>
#include <gxf/std/timestamp.hpp>
#include <isaac_ros_nitros/types/type_adapter_nitros_context.hpp>
#include <messages/point_cloud_message.hpp>
#endif
#endif

namespace orbbec_camera_transport_test {

inline void check_cuda(cudaError_t status, const char* operation) {
  if (status != cudaSuccess) {
    throw std::runtime_error(std::string(operation) + ": " + cudaGetErrorString(status));
  }
}

#if defined(TRANSPORT_CUDA_BUFFER)
class CudaReceiver {
 public:
  CudaReceiver() {
    check_cuda(cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking), "Create CUDA stream");
  }

  ~CudaReceiver() {
    (void)cudaStreamSynchronize(stream_);
    (void)cudaFree(upload_);
    (void)cudaStreamDestroy(stream_);
  }

  CudaReceiver(const CudaReceiver&) = delete;
  CudaReceiver& operator=(const CudaReceiver&) = delete;

  void read(const rosidl::Buffer<uint8_t>& data, bool gpu) {
    if (gpu) {
      auto handle = cuda_buffer_backend::from_input_buffer(data, stream_);
      if (!handle.get_ptr()) {
        throw std::runtime_error("CUDA read handle has no device pointer");
      }
      check_cuda(cudaStreamSynchronize(stream_), "Wait for shared CUDA buffer");
      return;
    }
    if (data.size() > capacity_) {
      if (upload_) {
        check_cuda(cudaFree(upload_), "Release CPU upload buffer");
        upload_ = nullptr;
        capacity_ = 0;
      }
      check_cuda(cudaMalloc(&upload_, data.size()), "Allocate CPU upload buffer");
      capacity_ = data.size();
    }
    check_cuda(cudaMemcpyAsync(upload_, data.data(), data.size(), cudaMemcpyHostToDevice, stream_),
               "Upload CPU baseline");
    check_cuda(cudaStreamSynchronize(stream_), "Wait for CPU baseline upload");
  }

 private:
  cudaStream_t stream_ = nullptr;
  void* upload_ = nullptr;
  size_t capacity_ = 0;
};
#else
namespace nitros = nvidia::isaac_ros::nitros;

inline void verify_device_pointer(const void* pointer, bool verify_read) {
  if (!pointer) {
    throw std::runtime_error("NITROS buffer has no device pointer");
  }
  if (verify_read) {
    uint8_t data = 0;
    check_cuda(cudaMemcpy(&data, pointer, sizeof(data), cudaMemcpyDeviceToHost),
               "Verify NITROS buffer");
  }
}

#if defined(TRANSPORT_NITROS_GXF)
namespace gxf = nvidia::gxf;

inline gxf::Entity retain_entity(gxf_uid_t handle) {
  auto entity = gxf::Entity::Shared(nitros::GetTypeAdapterNitrosContext().getContext(), handle);
  if (!entity) {
    throw std::runtime_error("Cannot retain the NITROS entity");
  }
  return std::move(entity.value());
}

inline int64_t nitros_stamp(const nitros::NitrosImage& message, bool verify_read) {
  auto entity = retain_entity(message.handle);
  auto video = entity.get<gxf::VideoBuffer>();
  auto timestamp = entity.get<gxf::Timestamp>("timestamp");
  if (!video || !timestamp) {
    throw std::runtime_error("NITROS image has no video buffer or timestamp");
  }
  verify_device_pointer(video.value()->pointer(), verify_read);
  return timestamp.value()->acqtime;
}

inline int64_t nitros_stamp(const nitros::NitrosPointCloud& message, bool verify_read) {
  auto entity = retain_entity(message.handle);
  auto parts = nvidia::isaac_ros::messages::GetPointCloudMessage(entity);
  if (!parts) {
    throw std::runtime_error("Invalid NITROS point cloud entity");
  }
  auto pointer = parts.value().points->data<float>();
  verify_device_pointer(pointer ? pointer.value() : nullptr, verify_read);
  return parts.value().timestamp->acqtime;
}
#else
template <typename Message>
int64_t nitros_stamp(const Message& message, bool verify_read) {
  auto handle = message.get_read_handle(nullptr);
  verify_device_pointer(handle.get_ptr(), verify_read);
  return static_cast<int64_t>(message.timestamp_sec) * 1000000000 + message.timestamp_nsec;
}
#endif
#endif

}  // namespace orbbec_camera_transport_test
#endif
