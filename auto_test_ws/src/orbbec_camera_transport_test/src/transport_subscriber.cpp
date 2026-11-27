// Copyright 2026 Orbbec Inc.
// SPDX-License-Identifier: Apache-2.0

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <optional>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/point_cloud2.hpp>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <vector>

#include "cuda_benchmark_system_load.hpp"
#include "gpu_transport_receiver.hpp"

namespace orbbec_camera_transport_test {
namespace {
using Clock = std::chrono::steady_clock;
using WallClock = std::chrono::system_clock;
using Seconds = std::chrono::duration<double>;
using Strings = std::vector<std::string>;

#if defined(TRANSPORT_CUDA_BUFFER)
constexpr char compiled_backend[] = "CUDA_BUFFER";
#elif defined(TRANSPORT_NITROS_BUFFER)
constexpr char compiled_backend[] = "NITROS_BUFFER";
#elif defined(TRANSPORT_NITROS_GXF)
constexpr char compiled_backend[] = "NITROS_GXF";
#else
constexpr char compiled_backend[] = "NONE";
#endif

int64_t wall_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(WallClock::now().time_since_epoch())
      .count();
}

struct Measurements {
  size_t samples = 0;
  size_t recent_samples = 0;
  size_t wrong_delivery = 0;
  size_t invalid_stamps = 0;
  size_t read_errors = 0;
  size_t cpu_fallback = 0;
  size_t window_latency_count = 0;
  double latency_sum = 0.0;
  double window_latency_sum = 0.0;
  double longest_gap = 0.0;
  Clock::time_point last_sample{};
  std::vector<double> latencies;
};

struct Stream {
  std::string topic;
  std::string camera;
  std::string name;
  std::string gpu_topic;
  bool required;
  bool points;
  bool host_clock;
  rclcpp::SubscriptionBase::SharedPtr subscription;
  size_t warmup = 0;
  bool verified = false;
  Measurements stats;
};

struct LoadAverage {
  double sum = 0;
  size_t count = 0;

  void add(std::optional<double> value) {
    if (value) {
      sum += *value;
      ++count;
    }
  }

  std::optional<double> value() const {
    return count ? std::optional<double>{sum / count} : std::nullopt;
  }
};

constexpr char csv_header[] =
    "elapsed_seconds,topic,samples,current_fps,avg_fps,latency_avg_ms,latency_p95_ms,"
    "invalid_stamps,delivery_mismatches,transport_reliability,phase,camera_name,stream,backend,"
    "read_errors,cpu_fallback_frames,longest_gap_sec,system_cpu_pct,system_gpu_pct,complete,"
    "warmup_received\n";

void write_number(std::ostream& stream, std::optional<double> value) {
  if (value) {
    stream << *value;
  }
}

void atomic_write(const std::filesystem::path& path, const std::string& contents) {
  const auto temporary = path.string() + ".tmp";
  std::ofstream file(temporary);
  file.exceptions(std::ios::failbit | std::ios::badbit);
  file << contents;
  file.close();
  std::filesystem::rename(temporary, path);
}
}  // namespace

class TransportSubscriber : public rclcpp::Node {
 public:
  explicit TransportSubscriber(const rclcpp::NodeOptions& options)
      : Node("transport_subscriber", options) {
    const auto mode = declare_parameter<std::string>("transport_mode", "cross_process");
    if (mode != "cross_process" && mode != "intra_process") {
      throw std::invalid_argument("transport_mode must be cross_process or intra_process");
    }
    intra_process_ = mode == "intra_process";
    backend_ = declare_parameter<std::string>("gpu_backend", "NONE");
    if (gpu_test() && backend_ != compiled_backend) {
      throw std::invalid_argument("Test subscriber was not built for " + backend_);
    }
    reliability_ = declare_parameter<std::string>("transport_reliability", "best_effort");
    if (reliability_ == "reliable") {
      qos_.reliable();
    } else if (reliability_ != "best_effort") {
      throw std::invalid_argument("transport_reliability must be best_effort or reliable");
    }
    qos_.keep_last(1);
    const auto seconds = declare_parameter<double>("duration_seconds", 60.0);
    if (!std::isfinite(seconds) || seconds <= 0.0) {
      throw std::invalid_argument("duration_seconds must be positive and finite");
    }
    duration_ = Seconds(seconds);
    const auto warmup_frames = declare_parameter<int64_t>("warmup_frames", 30);
    if (warmup_frames < (gpu_test() ? 0 : 1)) {
      throw std::invalid_argument("Invalid warmup_frames");
    }
    warmup_frames_ = static_cast<size_t>(warmup_frames);
    const auto warmup_timeout = declare_parameter<double>("warmup_timeout_sec", 60.0);
    if (!std::isfinite(warmup_timeout) || warmup_timeout <= 0.0) {
      throw std::invalid_argument("warmup_timeout_sec must be positive and finite");
    }
    warmup_timeout_ = Seconds(warmup_timeout);
    const auto directory = declare_parameter<std::string>("results_dir", "");
    if (directory.empty()) {
      throw std::invalid_argument("results_dir is required");
    }
    results_dir_ = directory;
    configure_streams();

#if defined(TRANSPORT_CUDA_BUFFER) || defined(TRANSPORT_NITROS_BUFFER) || \
    defined(TRANSPORT_NITROS_GXF)
    if (gpu_test()) {
      int device = 0;
      std::array<char, 32> pci_bus_id{};
      check_cuda(cudaGetDevice(&device), "Get the selected CUDA device");
      (void)cudaDeviceGetPCIBusId(pci_bus_id.data(), pci_bus_id.size(), device);
      system_load_.emplace(pci_bus_id.data());
#if defined(TRANSPORT_CUDA_BUFFER)
      if (backend_ == "CUDA_BUFFER") {
        cuda_reader_.emplace();
      }
#endif
    }
#endif
    first_stamp_ns_ = wall_ns();
    start_phase();
    timer_ = create_wall_timer(std::chrono::milliseconds(100), [this]() { tick(); });
  }

 private:
  bool gpu_test() const { return backend_ != "NONE"; }
  bool gpu_phase() const { return phase_ == 1; }
  bool ipc_phase() const {
    return gpu_test() ? gpu_phase() && backend_ != "CUDA_BUFFER" : intra_process_;
  }

  const char* phase_name() const {
    if (gpu_test()) {
      return !gpu_phase()                ? "CPU (DDS)"
             : backend_ == "CUDA_BUFFER" ? "CUDA Buffer"
                                         : "NITROS (IPC)";
    }
    return intra_process_ ? "IPC" : "DDS";
  }

  void configure_streams() {
    const auto topics = declare_parameter<Strings>("topics", Strings{});
    const auto required = declare_parameter<Strings>("required_topics", Strings{});
    const auto cameras = declare_parameter<Strings>("camera_names", Strings{});
    const auto names = declare_parameter<Strings>("stream_names", Strings{});
    const auto kinds = declare_parameter<Strings>("stream_kinds", Strings{});
    const auto gpu_topics = declare_parameter<Strings>("gpu_topics", Strings{});
    const auto host_clocks =
        declare_parameter<std::vector<bool>>("host_clocks", std::vector<bool>{});
    if (topics.empty()) {
      throw std::invalid_argument("At least one image or point cloud topic is required");
    }
    for (const auto* values : {&cameras, &names, &kinds, &gpu_topics}) {
      if (!values->empty() && values->size() != topics.size()) {
        throw std::invalid_argument("Provide one stream descriptor per topic");
      }
    }
    if ((!host_clocks.empty() && host_clocks.size() != topics.size()) ||
        (gpu_test() && gpu_topics.size() != topics.size())) {
      throw std::invalid_argument(
          "Provide clock and GPU topic information for all configured streams");
    }
    const std::unordered_set<std::string> known(topics.begin(), topics.end());
    if (known.size() != topics.size() ||
        std::any_of(required.begin(), required.end(),
                    [&](const auto& topic) { return !known.count(topic); })) {
      throw std::invalid_argument("Topics must be unique and include all required topics");
    }
    for (size_t index = 0; index < topics.size(); ++index) {
      const auto& topic = topics[index];
      const bool points = kinds.empty() ? topic.substr(topic.find_last_of('/') + 1) == "points"
                                        : kinds[index] == "points";
      streams_.push_back(
          Stream{topic,
                 cameras.empty() ? "" : cameras[index],
                 names.empty() ? "" : names[index],
                 gpu_topics.empty() ? topic : gpu_topics[index],
                 std::find(required.begin(), required.end(), topic) != required.end(),
                 points,
                 host_clocks.empty() || host_clocks[index],
                 {},
                 0,
                 false,
                 {}});
    }
  }

  void start_phase() {
    sampling_start_.reset();
    warmup_start_ = Clock::now();
    for (auto& stream : streams_) {
      stream.warmup = 0;
      stream.verified = false;
      stream.stats = {};
      subscribe(stream);
      RCLCPP_INFO(get_logger(), "[%s] Subscribing to %s", phase_name(),
                  stream.subscription->get_topic_name());
    }
    RCLCPP_INFO(get_logger(), "Starting %s phase, %s QoS, warmup=%zu frames for %.3f s of sampling",
                phase_name(), reliability_.c_str(), warmup_frames_, duration_.count());
  }

  void subscribe(Stream& stream) {
#if defined(TRANSPORT_NITROS_BUFFER) || defined(TRANSPORT_NITROS_GXF)
    if (gpu_phase()) {
      if (stream.points) {
        subscribe<nitros::NitrosPointCloud, true>(stream, stream.gpu_topic);
      } else {
        subscribe<nitros::NitrosImage, true>(stream, stream.gpu_topic);
      }
      return;
    }
#endif
    if (stream.points) {
      subscribe<sensor_msgs::msg::PointCloud2>(stream, stream.topic);
    } else {
      subscribe<sensor_msgs::msg::Image>(stream, stream.topic);
    }
  }

  template <typename Message, bool Nitros = false>
  void subscribe(Stream& stream, const std::string& topic) {
    rclcpp::SubscriptionOptions options;
    options.use_intra_process_comm =
        ipc_phase() ? rclcpp::IntraProcessSetting::Enable : rclcpp::IntraProcessSetting::Disable;
#if defined(TRANSPORT_CUDA_BUFFER)
    if (cuda_reader_) {
      options.acceptable_buffer_backends = gpu_phase() ? "cuda" : "cpu";
    }
#endif
    stream.subscription = create_subscription<Message>(
        topic, qos_,
        [this, &stream, phase = phase_](typename Message::ConstSharedPtr message,
                                        const rclcpp::MessageInfo& info) {
          const auto received = Clock::now();
          const auto host_ns = wall_ns();
          if (phase != phase_ || !stream.subscription ||
              (sampling_start_ && Seconds(received - *sampling_start_) >= duration_)) {
            return;
          }
          if (info.get_rmw_message_info().from_intra_process != ipc_phase()) {
            ++stream.stats.wrong_delivery;
            return;
          }
          try {
            const auto stamp = read_frame<Message, Nitros>(stream, *message);
            if (stamp) {
              observe(stream, received, host_ns, *stamp);
            }
          } catch (const std::exception& error) {
            ++stream.stats.read_errors;
            RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 2000, "[%s] %s: %s", phase_name(),
                                 stream.topic.c_str(), error.what());
          }
        },
        options);
  }

  template <typename Message, bool Nitros>
  std::optional<int64_t> read_frame(Stream& stream, const Message& message) {
    if constexpr (Nitros) {
#if defined(TRANSPORT_NITROS_BUFFER) || defined(TRANSPORT_NITROS_GXF)
      return nitros_stamp(message, !stream.verified);
#else
      return std::nullopt;
#endif
    } else {
      if (!message.data.size()) {
        return std::nullopt;
      }
#if defined(TRANSPORT_CUDA_BUFFER)
      if (cuda_reader_) {
        const auto actual = message.data.get_backend_type();
        if (actual != (gpu_phase() ? "cuda" : "cpu")) {
          if (gpu_phase() && actual == "cpu") {
            ++stream.stats.cpu_fallback;
            return std::nullopt;
          }
          throw std::runtime_error("Unexpected buffer backend: " + actual);
        }
        cuda_reader_->read(message.data, gpu_phase());
      }
#endif
      const auto& stamp = message.header.stamp;
      return static_cast<int64_t>(stamp.sec) * 1000000000 + stamp.nanosec;
    }
  }

  void observe(Stream& stream, Clock::time_point received, int64_t host_ns, int64_t frame_ns) {
    stream.verified = true;
    if (!sampling_start_) {
      ++stream.warmup;
      return;
    }
    auto& stats = stream.stats;
    const auto previous = stats.samples ? stats.last_sample : *sampling_start_;
    stats.longest_gap = std::max(stats.longest_gap, Seconds(received - previous).count());
    stats.last_sample = received;
    ++stats.samples;
    ++stats.recent_samples;
    if (!stream.host_clock || frame_ns < first_stamp_ns_ || frame_ns > host_ns) {
      ++stats.invalid_stamps;
      return;
    }
    const double latency_ms = (host_ns - frame_ns) / 1e6;
    stats.latencies.push_back(latency_ms);
    stats.latency_sum += latency_ms;
    ++stats.window_latency_count;
    stats.window_latency_sum += latency_ms;
  }

  void tick() {
    try {
      const auto now = Clock::now();
      if (!sampling_start_) {
        wait_for_streams(now);
        return;
      }
      const double elapsed = Seconds(now - *sampling_start_).count();
      const double window = Seconds(now - last_progress_).count();
      if (elapsed >= duration_.count()) {
        finish_phase();
      } else if (window >= 1.0) {
        sample_load();
        write_progress(elapsed, window);
        last_progress_ = now;
      }
    } catch (const std::exception& error) {
      RCLCPP_ERROR(get_logger(), "Could not complete transport benchmark: %s", error.what());
      get_node_base_interface()->get_context()->shutdown("transport output error");
    }
  }

  void wait_for_streams(Clock::time_point now) {
    if (Seconds(now - warmup_start_) >= warmup_timeout_) {
      for (const auto& stream : streams_) {
        if (stream.required || stream.warmup || stream.stats.wrong_delivery ||
            count_publishers(stream.subscription->get_topic_name()) > 0) {
          RCLCPP_ERROR(get_logger(),
                       "[%s] %s warmup=%zu/%zu, delivery mismatches=%zu, "
                       "CPU fallback=%zu, read errors=%zu",
                       phase_name(), stream.subscription->get_topic_name(), stream.warmup,
                       warmup_frames_, stream.stats.wrong_delivery, stream.stats.cpu_fallback,
                       stream.stats.read_errors);
        }
      }
      RCLCPP_ERROR(get_logger(), "Timed out waiting for all selected streams");
      atomic_write(results_dir_ / "transport_phase_summary.csv",
                   completed_rows_ + rows(0.0, 0.0, false));
      get_node_base_interface()->get_context()->shutdown("transport warmup timed out");
      return;
    }

    bool has_publisher = false;
    bool ready = true;
    for (const auto& stream : streams_) {
      const bool advertised = count_publishers(stream.subscription->get_topic_name()) > 0;
      has_publisher |= advertised || stream.warmup > 0;
      if (stream.required || advertised || stream.warmup > 0) {
        ready &= stream.verified && stream.warmup >= warmup_frames_;
      }
    }
    if (!has_publisher || !ready) {
      return;
    }
    for (auto& stream : streams_) {
      if (!stream.warmup) {
        stream.subscription.reset();
      }
      stream.stats = {};
    }
    if (system_load_) {
      system_load_->reset_cpu_baseline();
    }
    write_progress(0.0, 0.0);
    sampling_start_ = Clock::now();
    last_progress_ = *sampling_start_;
    RCLCPP_INFO(get_logger(), "[%s] All streams ready, sampling for %.3f s", phase_name(),
                duration_.count());
  }

  void sample_load() {
    if (system_load_) {
      const auto sample = system_load_->sample();
      last_load_ = sample;
      cpu_load_.add(sample.cpu_percent);
      gpu_load_.add(sample.gpu_percent);
    }
  }

  std::string rows(double elapsed, double window, bool final) {
    std::ostringstream file;
    file << std::fixed << std::setprecision(3);
    for (auto& stream : streams_) {
      if (!stream.subscription) {
        continue;
      }
      auto& stats = stream.stats;
      const auto fps = elapsed > 0 ? stats.samples / elapsed : 0.0;
      const auto recent_fps = window > 0 ? stats.recent_samples / window : 0.0;
      const bool complete = final && (gpu_test() ? stats.samples >= 2 && !stats.wrong_delivery &&
                                                       !stats.read_errors && !stats.cpu_fallback
                                                 : stats.samples > 0);
      file << elapsed << ',' << stream.topic << ',' << stats.samples << ',' << recent_fps << ','
           << fps << ',';
      if (gpu_test() && !final) {
        if (stats.window_latency_count) {
          file << stats.window_latency_sum / stats.window_latency_count;
        }
      } else if (!stats.latencies.empty()) {
        file << stats.latency_sum / stats.latencies.size();
      }
      file << ',';
      if (final && !stats.latencies.empty()) {
        const auto index = static_cast<size_t>(std::ceil(0.95 * stats.latencies.size())) - 1;
        std::nth_element(stats.latencies.begin(), stats.latencies.begin() + index,
                         stats.latencies.end());
        file << stats.latencies[index];
      }
      file << ',' << stats.invalid_stamps << ',' << stats.wrong_delivery << ',' << reliability_
           << ',' << phase_name() << ',' << stream.camera << ',' << stream.name << ',';
      if (stats.samples) {
        file << (gpu_phase() ? backend_ : "cpu");
      }
      file << ',' << stats.read_errors << ',' << stats.cpu_fallback << ',';
      const auto gap =
          final && stats.samples
              ? std::max(stats.longest_gap,
                         Seconds(*sampling_start_ + duration_ - stats.last_sample).count())
              : stats.longest_gap;
      file << gap << ',';
      write_number(file, final ? cpu_load_.value() : last_load_.cpu_percent);
      file << ',';
      write_number(file, final ? gpu_load_.value() : last_load_.gpu_percent);
      file << ',' << (complete ? "true" : "false") << ',' << stream.warmup << '\n';
      stats.recent_samples = 0;
      stats.window_latency_count = 0;
      stats.window_latency_sum = 0.0;
    }
    return file.str();
  }

  void write_progress(double elapsed, double window) {
    const auto path = results_dir_ / "transport_progress.csv";
    if (gpu_test()) {
      atomic_write(path, completed_rows_ + rows(elapsed, window, false));
      return;
    }
    const bool header = !std::filesystem::exists(path);
    std::ofstream file(path, std::ios::app);
    file.exceptions(std::ios::failbit | std::ios::badbit);
    if (header) {
      file << csv_header;
    }
    file << rows(elapsed, window, false);
  }

  void finish_phase() {
    const double window = duration_.count() - Seconds(last_progress_ - *sampling_start_).count();
    sample_load();
    completed_rows_ += rows(duration_.count(), window, true);
    atomic_write(results_dir_ / "transport_phase_summary.csv", completed_rows_);
    if (gpu_test() && !gpu_phase()) {
      for (auto& stream : streams_) {
        stream.subscription.reset();
      }
      ++phase_;
      cpu_load_ = {};
      gpu_load_ = {};
      last_load_ = {};
      start_phase();
      return;
    }
    timer_->cancel();
    atomic_write(results_dir_ / "transport_summary.csv", completed_rows_);
    RCLCPP_INFO(get_logger(), "Transport sampling completed");
    get_node_base_interface()->get_context()->shutdown("transport sampling finished");
  }

  bool intra_process_ = false;
  size_t phase_ = 0;
  std::string backend_;
  std::string reliability_;
  rclcpp::SensorDataQoS qos_;
  Seconds duration_{};
  size_t warmup_frames_ = 0;
  Seconds warmup_timeout_{};
  std::filesystem::path results_dir_;
  std::string completed_rows_ = csv_header;
  int64_t first_stamp_ns_ = 0;
  Clock::time_point warmup_start_;
  Clock::time_point last_progress_;
  std::optional<Clock::time_point> sampling_start_;
  std::vector<Stream> streams_;
  rclcpp::TimerBase::SharedPtr timer_;
  std::optional<CudaBenchmarkSystemLoad> system_load_;
  CudaBenchmarkSystemLoad::Sample last_load_;
  LoadAverage cpu_load_;
  LoadAverage gpu_load_;
#if defined(TRANSPORT_CUDA_BUFFER)
  std::optional<CudaReceiver> cuda_reader_;
#endif
};
}  // namespace orbbec_camera_transport_test

RCLCPP_COMPONENTS_REGISTER_NODE(orbbec_camera_transport_test::TransportSubscriber)
