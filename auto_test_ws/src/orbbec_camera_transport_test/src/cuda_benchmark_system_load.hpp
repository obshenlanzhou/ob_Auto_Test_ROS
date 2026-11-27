// Copyright 2026 Orbbec Inc.
// SPDX-License-Identifier: Apache-2.0

#pragma once

#include <dlfcn.h>

#include <array>
#include <cstdint>
#include <fstream>
#include <numeric>
#include <optional>
#include <string>

namespace orbbec_camera_transport_test {

class CudaBenchmarkSystemLoad {
 public:
  struct Sample {
    std::optional<double> cpu_percent;
    std::optional<double> gpu_percent;
  };

  explicit CudaBenchmarkSystemLoad(const char* pci_bus_id) {
    nvml_ = dlopen("libnvidia-ml.so.1", RTLD_LAZY | RTLD_LOCAL);
    if (!nvml_) {
      return;
    }
    const auto init = reinterpret_cast<int (*)()>(dlsym(nvml_, "nvmlInit_v2"));
    shutdown_ = reinterpret_cast<int (*)()>(dlsym(nvml_, "nvmlShutdown"));
    const auto get_device = reinterpret_cast<int (*)(const char*, void**)>(
        dlsym(nvml_, "nvmlDeviceGetHandleByPciBusId_v2"));
    get_utilization_ = reinterpret_cast<int (*)(void*, NvmlUtilization*)>(
        dlsym(nvml_, "nvmlDeviceGetUtilizationRates"));
    if (init && shutdown_ && get_device && get_utilization_ && init() == 0) {
      initialized_ = true;
      if (get_device(pci_bus_id, &gpu_) != 0) {
        gpu_ = nullptr;
      }
    }
  }

  ~CudaBenchmarkSystemLoad() {
    if (initialized_) {
      shutdown_();
    }
    if (nvml_) {
      dlclose(nvml_);
    }
  }

  CudaBenchmarkSystemLoad(const CudaBenchmarkSystemLoad&) = delete;
  CudaBenchmarkSystemLoad& operator=(const CudaBenchmarkSystemLoad&) = delete;

  void reset_cpu_baseline() { previous_cpu_ = read_cpu_ticks(); }

  Sample sample() {
    Sample load;
    const auto cpu = read_cpu_ticks();
    if (cpu && previous_cpu_ && cpu->total > previous_cpu_->total &&
        cpu->idle >= previous_cpu_->idle) {
      const auto total = cpu->total - previous_cpu_->total;
      const auto idle = cpu->idle - previous_cpu_->idle;
      if (idle <= total) {
        load.cpu_percent = 100.0 * static_cast<double>(total - idle) / total;
      }
    }
    previous_cpu_ = cpu;

    NvmlUtilization utilization{};
    if (gpu_ && get_utilization_(gpu_, &utilization) == 0) {
      load.gpu_percent = utilization.gpu;
    } else {
      // Integrated Jetson GPUs may expose load in tenths of a percent.
      std::ifstream file("/sys/devices/platform/gpu.0/load");
      unsigned permille = 0;
      if (file >> permille && permille <= 1000) {
        load.gpu_percent = permille / 10.0;
      }
    }
    return load;
  }

 private:
  struct CpuTicks {
    uint64_t total;
    uint64_t idle;
  };

  struct NvmlUtilization {
    unsigned int gpu;
    unsigned int memory;
  };

  static std::optional<CpuTicks> read_cpu_ticks() {
    std::ifstream file("/proc/stat");
    std::string name;
    if (!(file >> name) || name != "cpu") {
      return std::nullopt;
    }
    // The first eight counters exclude guest ticks, which are included in user/nice.
    std::array<uint64_t, 8> ticks{};
    for (auto& value : ticks) {
      if (!(file >> value)) {
        return std::nullopt;
      }
    }
    return CpuTicks{std::accumulate(ticks.begin(), ticks.end(), uint64_t{0}), ticks[3] + ticks[4]};
  }

  std::optional<CpuTicks> previous_cpu_;
  void* nvml_ = nullptr;
  void* gpu_ = nullptr;
  int (*get_utilization_)(void*, NvmlUtilization*) = nullptr;
  int (*shutdown_)() = nullptr;
  bool initialized_ = false;
};

}  // namespace orbbec_camera_transport_test
