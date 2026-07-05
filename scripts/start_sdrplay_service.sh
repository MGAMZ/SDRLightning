#!/usr/bin/env bash
#
# scripts/start_sdrplay_service.sh
#
# 【Linux / WSL2 only】 启动 SDRplay API 的 IPC 服务守护进程.
# Windows 上 SDRplay API 安装为 Windows service, 自动启动, 不需要这个脚本.
#
# Linux 下它需要 root 权限（直接访问 USB），所以要 sudo 跑。
#
# 用法:
#     sudo bash scripts/start_sdrplay_service.sh
#
# 启动后这个进程会一直跑（前台）。通常用法是在另一个终端 nohup & 起来：
#
#     sudo nohup bash scripts/start_sdrplay_service.sh > /tmp/sdrplay_service.log 2>&1 &
#

set -euo pipefail

_THIS="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "${_THIS}/.." && pwd)"
API_DIR="${ROOT}/third_party/sdrplay_api"
DAEMON="${API_DIR}/extract/amd64/sdrplay_apiService"

if [ ! -x "${DAEMON}" ]; then
    echo "[x] 找不到 ${DAEMON}" >&2
    echo "    请确认 third_party/sdrplay_api/extract/amd64/sdrplay_apiService 存在" >&2
    exit 1
fi

if pgrep -x sdrplay_apiService >/dev/null 2>&1; then
    echo "[i] sdrplay_apiService 已经在跑 (pid=$(pgrep -x sdrplay_apiService))"
    exit 0
fi

echo "[+] 启动 sdrplay_apiService ..."
echo "    日志会打到 stdout,Ctrl+C 停止"
exec "${DAEMON}"