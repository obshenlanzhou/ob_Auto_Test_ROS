#!/usr/bin/env bash

set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_ROOT="${WORKSPACE_ROOT}/src/orbbec_camera_auto_test_ui"
CORE_PACKAGE_ROOT="${WORKSPACE_ROOT}/src/orbbec_camera_auto_test"

export PYTHONPATH="${UI_ROOT}:${CORE_PACKAGE_ROOT}:${PYTHONPATH:-}"

python3 -m orbbec_camera_auto_test_ui.server "$@"
