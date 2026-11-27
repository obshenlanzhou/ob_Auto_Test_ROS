#!/usr/bin/env bash

set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_ROOT="${WORKSPACE_ROOT}/src/orbbec_camera_auto_test_ui"
CORE_PACKAGE_ROOT="${WORKSPACE_ROOT}/src/orbbec_camera_auto_test"

if ! command -v git >/dev/null && [[ -t 0 && -t 2 ]] \
    && [[ "${1:-}" != "--help" && "${1:-}" != "-h" ]] \
    && command -v apt-get >/dev/null; then
  if read -r -p '未找到 Git。安装 Git 以启用在线更新？[y/N] ' install_git \
      && [[ "$install_git" == [yY] ]]; then
    git_installer=(apt-get install -y git)
    if (( EUID != 0 )); then
      git_installer=(sudo "${git_installer[@]}")
    fi
    if ! "${git_installer[@]}"; then
      printf '%s\n' 'Git 安装失败，可以继续打开 Web UI。' >&2
    fi
  fi
fi

export PYTHONPATH="${UI_ROOT}:${CORE_PACKAGE_ROOT}:${PYTHONPATH:-}"

exec python3 -m orbbec_camera_auto_test_ui.server "$@"
