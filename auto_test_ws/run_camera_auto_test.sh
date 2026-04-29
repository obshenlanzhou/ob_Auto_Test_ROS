#!/usr/bin/env bash

set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="${WORKSPACE_ROOT}/src/orbbec_camera_auto_test"
PYTHONPATH="${PACKAGE_ROOT}:${PYTHONPATH:-}"
export PYTHONPATH

MODE=""
DURATION=""
PROFILE=""
PERFORMANCE_SCENARIO=""
STABLE_SECONDS="10"
STREAM_TIMEOUT="60"
MAX_GAP_SECONDS="1.5"
RESTART_DELAY="2"
CAMERA_NAME=""
SERIAL_NUMBER=""
USB_PORT=""
CONFIG_FILE_PATH=""
DRIVER_SETUP="${ORBBEC_DRIVER_SETUP:-}"
RESULTS_ROOT="${WORKSPACE_ROOT}/results"
LAUNCH_FILE=""
LAUNCH_ARGS=()
IMAGE_TOPICS=()

usage() {
  cat <<'EOF'
Usage:
  run_camera_auto_test.sh --mode functional
  run_camera_auto_test.sh --mode performance [--performance-scenario NAME] [--duration 300]
  run_camera_auto_test.sh --mode restart --duration 300 [--image-topic /camera/color/image_raw]
  run_camera_auto_test.sh --mode all [--performance-scenario NAME] [--duration 300]

Options:
  --mode functional|performance|restart|all
  --duration SECONDS   Performance duration or restart stress duration
  --profile PROFILE_NAME_OR_PATH
  --performance-scenario NAME
  --stable-seconds SECONDS
  --stream-timeout SECONDS
  --max-gap-seconds SECONDS
  --restart-delay SECONDS
  --image-topic TOPIC
  --camera-name NAME
  --serial-number SERIAL
  --usb-port PORT
  --config-file-path PATH
  --driver-setup PATH
  --results-root PATH
  --launch-file FILE
  --launch-arg KEY=VALUE
EOF
}

safe_source() {
  local setup_file="$1"
  set +u
  # shellcheck disable=SC1090
  source "${setup_file}"
  set -u
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    --duration)
      DURATION="$2"
      shift 2
      ;;
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --performance-scenario)
      PERFORMANCE_SCENARIO="$2"
      shift 2
      ;;
    --stable-seconds)
      STABLE_SECONDS="$2"
      shift 2
      ;;
    --stream-timeout)
      STREAM_TIMEOUT="$2"
      shift 2
      ;;
    --max-gap-seconds)
      MAX_GAP_SECONDS="$2"
      shift 2
      ;;
    --restart-delay)
      RESTART_DELAY="$2"
      shift 2
      ;;
    --image-topic)
      IMAGE_TOPICS+=("$2")
      shift 2
      ;;
    --camera-name)
      CAMERA_NAME="$2"
      shift 2
      ;;
    --serial-number)
      SERIAL_NUMBER="$2"
      shift 2
      ;;
    --usb-port)
      USB_PORT="$2"
      shift 2
      ;;
    --config-file-path)
      CONFIG_FILE_PATH="$2"
      shift 2
      ;;
    --driver-setup)
      DRIVER_SETUP="$2"
      shift 2
      ;;
    --results-root)
      RESULTS_ROOT="$2"
      shift 2
      ;;
    --launch-file)
      LAUNCH_FILE="$2"
      shift 2
      ;;
    --launch-arg)
      LAUNCH_ARGS+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${MODE}" ]]; then
  echo "--mode is required" >&2
  usage
  exit 1
fi

if [[ ! -f /opt/ros/humble/setup.bash ]]; then
  echo "ROS2 Humble setup not found at /opt/ros/humble/setup.bash" >&2
  exit 1
fi

unset ROS_DISTRO
unset ROS_ETC_DIR
safe_source /opt/ros/humble/setup.bash
if [[ -n "${DRIVER_SETUP}" ]]; then
  if [[ ! -f "${DRIVER_SETUP}" ]]; then
    echo "Driver setup file not found: ${DRIVER_SETUP}" >&2
    exit 1
  fi
  safe_source "${DRIVER_SETUP}"
fi

RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_ROOT="${RESULTS_ROOT}/${RUN_ID}"
mkdir -p "${RUN_ROOT}"

COMMON_ARGS=(
  --results-dir ""
)

if [[ -n "${PROFILE}" ]]; then
  COMMON_ARGS+=(--profile "${PROFILE}")
fi
if [[ -n "${CAMERA_NAME}" ]]; then
  COMMON_ARGS+=(--camera-name "${CAMERA_NAME}")
fi
if [[ -n "${SERIAL_NUMBER}" ]]; then
  COMMON_ARGS+=(--serial-number "${SERIAL_NUMBER}")
fi
if [[ -n "${USB_PORT}" ]]; then
  COMMON_ARGS+=(--usb-port "${USB_PORT}")
fi
if [[ -n "${CONFIG_FILE_PATH}" ]]; then
  COMMON_ARGS+=(--config-file-path "${CONFIG_FILE_PATH}")
fi
if [[ -n "${DRIVER_SETUP}" ]]; then
  COMMON_ARGS+=(--driver-setup "${DRIVER_SETUP}")
fi
if [[ -n "${LAUNCH_FILE}" ]]; then
  COMMON_ARGS+=(--launch-file "${LAUNCH_FILE}")
fi
for launch_arg in "${LAUNCH_ARGS[@]}"; do
  COMMON_ARGS+=(--launch-arg "${launch_arg}")
done

run_functional() {
  local functional_dir="${RUN_ROOT}/functional"
  mkdir -p "${functional_dir}"
  local args=("${COMMON_ARGS[@]}")
  args[1]="${functional_dir}"
  python3 -m orbbec_camera_auto_test.functional_runner "${args[@]}"
}

run_performance() {
  local performance_dir="${RUN_ROOT}/performance"
  mkdir -p "${performance_dir}"
  local args=("${COMMON_ARGS[@]}")
  args[1]="${performance_dir}"
  if [[ -n "${PERFORMANCE_SCENARIO}" ]]; then
    args+=(--performance-scenario "${PERFORMANCE_SCENARIO}")
  fi
  if [[ -n "${DURATION}" ]]; then
    args+=(--duration "${DURATION}")
  fi
  python3 -m orbbec_camera_auto_test.performance_runner "${args[@]}"
}

run_restart() {
  local restart_dir="${RUN_ROOT}/restart"
  mkdir -p "${restart_dir}"
  local args=("${COMMON_ARGS[@]}")
  args[1]="${restart_dir}"
  if [[ -n "${PERFORMANCE_SCENARIO}" ]]; then
    args+=(--performance-scenario "${PERFORMANCE_SCENARIO}")
  fi
  if [[ -n "${DURATION}" ]]; then
    args+=(--duration "${DURATION}")
  fi
  args+=(--stable-seconds "${STABLE_SECONDS}")
  args+=(--stream-timeout "${STREAM_TIMEOUT}")
  args+=(--max-gap-seconds "${MAX_GAP_SECONDS}")
  args+=(--restart-delay "${RESTART_DELAY}")
  for image_topic in "${IMAGE_TOPICS[@]}"; do
    args+=(--image-topic "${image_topic}")
  done
  python3 -m orbbec_camera_auto_test.restart_runner "${args[@]}"
}

case "${MODE}" in
  functional)
    run_functional
    ;;
  performance)
    run_performance
    ;;
  restart)
    run_restart
    ;;
  all)
    run_functional
    run_performance
    ;;
  *)
    echo "Unsupported mode: ${MODE}" >&2
    exit 1
    ;;
esac
