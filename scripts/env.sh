#!/usr/bin/env bash
#
# scripts/env.sh
#
# 【Linux / WSL2 only】 设置 SDR 开发环境变量。Windows 上请用 scripts/env.ps1。
#
# 用法:
#     source scripts/env.sh
#
# 设的变量:
#   SOAPY_SDR_PLUGIN_PATH  让 SoapySDR 找到本地装的 libsdrPlaySupport.so
#   LD_LIBRARY_PATH        让动态链接器找到 libsdrplay_api.so (备用)
#
# 备注:
#   现在 scripts/_env.py 在 Python import 时自动处理这些事, 多数情况下
#   不再需要 source 此脚本. 留着它只是为了向后兼容.
#

# 这个脚本的绝对路径
_THIS="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "${_THIS}/.." && pwd)"

# SoapySDR 插件搜索路径（本地 install 前缀 + 系统默认）
export SOAPY_SDR_PLUGIN_PATH="${ROOT}/third_party/install/lib/SoapySDR/modules0.8:${SOAPY_SDR_PLUGIN_PATH:-}"

# 备用：本地 install 的 lib（这里目前是空的，但留个口子）
LOCAL_LIB="${ROOT}/third_party/install/lib"
if [ -d "${LOCAL_LIB}" ] && ls "${LOCAL_LIB}"/*.so* >/dev/null 2>&1; then
    export LD_LIBRARY_PATH="${LOCAL_LIB}:${LD_LIBRARY_PATH:-}"
fi

# 激活 conda sdr 环境
# shellcheck disable=SC1091
source /home/mgam/miniforge3/etc/profile.d/conda.sh
conda activate sdr

echo "[env] SOAPY_SDR_PLUGIN_PATH=${SOAPY_SDR_PLUGIN_PATH}"
echo "[env] sdr conda env: $(which python) ($(python --version 2>&1))"
echo "[env] SoapySDRUtil 现在能看到 sdrplay 模块:"
SoapySDRUtil --info 2>&1 | grep -E "Module found|sdrplay|sdrPlay" | head -5