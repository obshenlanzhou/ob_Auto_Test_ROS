// Copyright 2026 Orbbec Inc.
// SPDX-License-Identifier: Apache-2.0

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <numeric>
#include <optional>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/point_cloud2.hpp>
#include <stdexcept>
#include <string>
#include <vector>

namespace orbbec_camera_transport_test {
namespace {
using Clock = std::chrono::steady_clock;
using WallClock = std::chrono::system_clock;
using Seconds = std::chrono::duration<double>;

int64_t wall_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             WallClock::now().time_since_epoch())
      .count();
}

struct Stream {
  std::string topic;
  bool required;
  rclcpp::SubscriptionBase::SharedPtr subscription;
  size_t warmup = 0;
  size_t samples = 0;
  size_t recent_samples = 0;
  size_t wrong_delivery = 0;
  size_t invalid_stamps = 0;
  double latency_sum = 0.0;
  std::vector<double> latencies;
};

constexpr char csv_header[] =
    "elapsed_seconds,topic,samples,current_fps,avg_fps,latency_avg_ms,latency_"
    "p95_ms,"
    "invalid_stamps,delivery_mismatches,transport_reliability\n";
}  // namespace

class TransportSubscriber : public rclcpp::Node {
 public:
  explicit TransportSubscriber(const rclcpp::NodeOptions& options)
      : Node("transport_subscriber", options) {
    const auto mode =
        declare_parameter<std::string>("transport_mode", "cross_process");
    if (mode != "cross_process" && mode != "intra_process") {
      throw std::invalid_argument(
          "transport_mode must be cross_process or intra_process");
    }
    intra_process_ = mode == "intra_process";
    reliability_ = declare_parameter<std::string>("transport_reliability", "best_effort");
    rclcpp::SensorDataQoS qos;
    qos.keep_last(1);
    if (reliability_ == "reliable") {
      qos.reliable();
    } else if (reliability_ != "best_effort") {
      throw std::invalid_argument("transport_reliability must be best_effort or reliable");
    }
    const double seconds = declare_parameter<double>("duration_seconds", 60.0);
    if (!std::isfinite(seconds) || seconds <= 0.0) {
      throw std::invalid_argument(
          "duration_seconds must be positive and finite");
    }
    duration_ = Seconds(seconds);
    const auto warmup_frames = declare_parameter<int64_t>("warmup_frames", 30);
    if (warmup_frames <= 0) {
      throw std::invalid_argument("warmup_frames must be positive");
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
    using TopicList = std::vector<std::string>;
    const auto topics = declare_parameter<TopicList>("topics", TopicList{});
    const auto required_topics =
        declare_parameter<TopicList>("required_topics", TopicList{});
    if (topics.empty()) {
      throw std::invalid_argument(
          "At least one image or point cloud topic is required");
    }
    for (const auto& topic : topics) {
      const bool required =
          std::find(required_topics.begin(), required_topics.end(), topic) !=
          required_topics.end();
      streams_.push_back(Stream{topic, required, {}});
    }
    for (auto& stream : streams_) {
      if (stream.topic.substr(stream.topic.find_last_of('/')) == "/points") {
        subscribe<sensor_msgs::msg::PointCloud2>(stream, qos);
      } else {
        subscribe<sensor_msgs::msg::Image>(stream, qos);
      }
    }
    first_stamp_ns_ = wall_ns();
    warmup_start_ = Clock::now();
    timer_ =
        create_wall_timer(std::chrono::milliseconds(100), [this]() { tick(); });
    RCLCPP_INFO(get_logger(), "Subscription QoS: %s, volatile, keep_last(1)", reliability_.c_str());
    RCLCPP_INFO(get_logger(), "Waiting up to %.3f s for %s data and %zu warmup frames per stream",
                warmup_timeout_.count(), mode.c_str(), warmup_frames_);
  }

 private:
  template <typename Message>
  void subscribe(Stream& stream, const rclcpp::QoS& qos) {
    rclcpp::SubscriptionOptions options;
    options.use_intra_process_comm = intra_process_
                                         ? rclcpp::IntraProcessSetting::Enable
                                         : rclcpp::IntraProcessSetting::Disable;
    stream.subscription = create_subscription<Message>(
        stream.topic, qos,
        [this, &stream](typename Message::ConstSharedPtr message,
                        const rclcpp::MessageInfo& info) {
          const auto received = Clock::now();
          const auto received_wall_ns = wall_ns();
          if (!stream.subscription ||
              (sampling_start_ &&
               Seconds(received - *sampling_start_) >= duration_)) {
            return;
          }
          if (info.get_rmw_message_info().from_intra_process !=
              intra_process_) {
            ++stream.wrong_delivery;
            return;
          }
          if (message->data.empty()) {
            return;
          }
          if (!sampling_start_) {
            ++stream.warmup;
            return;
          }
          ++stream.samples;
          ++stream.recent_samples;
          const auto& stamp = message->header.stamp;
          const int64_t frame_ns =
              static_cast<int64_t>(stamp.sec) * 1000000000 + stamp.nanosec;
          // Live camera frames should be stamped during this run on the host
          // clock.
          if (frame_ns < first_stamp_ns_ || frame_ns > received_wall_ns) {
            ++stream.invalid_stamps;
            return;
          }
          const double latency_ms = (received_wall_ns - frame_ns) / 1e6;
          stream.latencies.push_back(latency_ms);
          stream.latency_sum += latency_ms;
        },
        options);
  }

  void tick() {
    const auto now = Clock::now();
    if (!sampling_start_) {
      wait_for_streams(now);
      return;
    }
    const auto elapsed = Seconds(now - *sampling_start_);
    if (elapsed >= duration_) {
      finish();
    } else if (Seconds(now - last_progress_).count() >= 1.0) {
      try {
        write_rows(progress_, elapsed.count(), Seconds(now - last_progress_).count(), false);
        progress_.flush();
        last_progress_ = now;
      } catch (const std::exception& error) {
        RCLCPP_ERROR(get_logger(), "Could not write progress: %s", error.what());
        get_node_base_interface()->get_context()->shutdown("transport output error");
      }
    }
  }

  void wait_for_streams(Clock::time_point now) {
    if (Seconds(now - warmup_start_) >= warmup_timeout_) {
      for (const auto& stream : streams_) {
        if (stream.required || stream.warmup || stream.wrong_delivery ||
            count_publishers(stream.topic) > 0) {
          RCLCPP_ERROR(
              get_logger(), "%s warmup=%zu/%zu, delivery mismatches=%zu",
              stream.topic.c_str(), stream.warmup, warmup_frames_, stream.wrong_delivery);
        }
      }
      RCLCPP_ERROR(get_logger(), "Timed out waiting for the selected streams after %.3f s",
                   warmup_timeout_.count());
      get_node_base_interface()->get_context()->shutdown(
          "transport warmup timed out");
      return;
    }

    bool has_publisher = false;
    bool ready = true;
    for (const auto& stream : streams_) {
      const bool advertised = count_publishers(stream.topic) > 0;
      has_publisher |= advertised || stream.warmup > 0;
      if (stream.required || advertised || stream.warmup > 0) {
        ready &= stream.warmup >= warmup_frames_;
      }
    }
    if (!has_publisher || !ready) {
      return;
    }
    for (auto& stream : streams_) {
      if (!stream.warmup) {
        stream.subscription.reset();
      }
    }
    try {
      progress_.open(results_dir_ / "transport_progress.csv");
      progress_.exceptions(std::ios::failbit | std::ios::badbit);
      progress_ << std::fixed << std::setprecision(3) << csv_header;
      write_rows(progress_, 0.0, 0.0, false);
      progress_.flush();
    } catch (const std::exception& error) {
      RCLCPP_ERROR(get_logger(), "Could not write progress: %s", error.what());
      get_node_base_interface()->get_context()->shutdown(
          "transport output error");
      return;
    }
    sampling_start_ = Clock::now();
    last_progress_ = *sampling_start_;
    RCLCPP_INFO(get_logger(), "All streams ready, sampling for %.3f s",
                duration_.count());
  }

  void write_rows(std::ostream& file, double elapsed, double window,
                  bool final) {
    for (auto& stream : streams_) {
      if (!stream.subscription) {
        continue;
      }
      const double recent_fps = window > 0 ? stream.recent_samples / window : 0;
      const double average_fps = elapsed > 0 ? stream.samples / elapsed : 0;
      file << elapsed << ',' << stream.topic << ',' << stream.samples << ','
           << recent_fps << ',' << average_fps << ',';
      if (!stream.latencies.empty()) {
        file << stream.latency_sum / stream.latencies.size();
      }
      file << ',';
      if (final && !stream.latencies.empty()) {
        const auto index =
            static_cast<size_t>(std::ceil(0.95 * stream.latencies.size())) - 1;
        std::nth_element(stream.latencies.begin(),
                         stream.latencies.begin() + index,
                         stream.latencies.end());
        file << stream.latencies[index];
      }
      file << ',' << stream.invalid_stamps << ',' << stream.wrong_delivery << ',' << reliability_
           << '\n';
      if (!final && elapsed > 0.0) {
        RCLCPP_INFO(get_logger(), "[%s] Sampling %.1f/%.1f s, samples=%zu, FPS=%.2f",
                    stream.topic.c_str(), elapsed, duration_.count(), stream.samples, recent_fps);
      }
      stream.recent_samples = 0;
    }
  }

  void finish() {
    timer_->cancel();
    try {
      const auto temporary = results_dir_ / "transport_summary.csv.tmp";
      std::ofstream report(temporary);
      report.exceptions(std::ios::failbit | std::ios::badbit);
      report << std::fixed << std::setprecision(3) << csv_header;
      const double window = duration_.count() - Seconds(last_progress_ - *sampling_start_).count();
      write_rows(report, duration_.count(), window, true);
      report.close();
      std::filesystem::rename(temporary,
                              results_dir_ / "transport_summary.csv");
      RCLCPP_INFO(get_logger(), "Transport sampling completed");
    } catch (const std::exception& error) {
      RCLCPP_ERROR(get_logger(), "Could not write transport summary: %s",
                   error.what());
    }
    progress_.close();
    get_node_base_interface()->get_context()->shutdown(
        "transport sampling finished");
  }

  bool intra_process_ = false;
  std::string reliability_;
  Seconds duration_{};
  size_t warmup_frames_ = 0;
  Seconds warmup_timeout_{};
  std::filesystem::path results_dir_;
  std::ofstream progress_;
  int64_t first_stamp_ns_ = 0;
  Clock::time_point warmup_start_;
  Clock::time_point last_progress_;
  std::optional<Clock::time_point> sampling_start_;
  std::vector<Stream> streams_;
  rclcpp::TimerBase::SharedPtr timer_;
};
}  // namespace orbbec_camera_transport_test

RCLCPP_COMPONENTS_REGISTER_NODE(
    orbbec_camera_transport_test::TransportSubscriber)
