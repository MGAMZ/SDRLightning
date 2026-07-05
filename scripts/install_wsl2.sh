#!/usr/bin/env bash
#
# install_wsl2.sh
#
# 【Linux / WSL2 only】 在 WSL2 (Ubuntu 24.04) 上安装 SDRplay RSP1 所需的全部工具链.
# Windows 上请看 docs/WINDOWS.md (装 SDRplay API MSI + PothosSDR/SoapySDRPlay3).
#
#   1. apt: SoapySDR + Python bindings + libusb + cmake + numpy/scipy/matplotlib
#   2. SDRplay 官方 Linux API  (libsdrplay_api.so)
#   3. SoapySDRPlay3  (从 pothosware github 源码编译)
#
# 用法: bash scripts/install_wsl2.sh
#
# 假设：
#   - 已运行过 `usbipd wsl attach --busid <X>` 把 RSP1 透传进 WSL2
#   - 用户在 Ubuntu 24.04 x86_64 环境
#

set -euo pipefail

# ===== 颜色输出 =====
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

# ===== 0. 预备 =====
step "检查是否为 WSL2/Ubuntu ..."
if ! grep -qi "microsoft\|WSL" /proc/version; then
    warn "看起来不是 WSL2，但脚本应该也能跑。继续。"
fi

# ===== 1. apt 包 =====
step "安装 apt 依赖 ..."
sudo apt-get update -qq
sudo apt-get install -y \
    soapysdr-tools \
    libsoapysdr0.8 \
    libsoapysdr-dev \
    python3-soapysdr \
    python3-numpy \
    python3-scipy \
    python3-matplotlib \
    libusb-1.0-0-dev \
    cmake \
    build-essential \
    git \
    wget \
    ca-certificates

# ===== 2. SDRplay 官方 API =====
# 优先尝试 apt 是否有（Ubuntu 仓库没有，但保留兼容）；否则从官网下载。
SDRPLAY_API_VERSION="${SDRPLAY_API_VERSION:-3.15.2}"
SDRPLAY_API_DEB_PKG="sdrplay-api-${SDRPLAY_API_VERSION}"

step "下载并安装 SDRplay API ${SDRPLAY_API_VERSION} ..."

cd /tmp
if dpkg -l | grep -q "sdrplay-api"; then
    warn "sdrplay-api 已经装过，跳过。"
else
    # 官方 .deb 下载地址（如果版本变了需要手动查 https://www.sdrplay.com/downloads/）
    API_DEB_URL="https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-${SDRPLAY_API_VERSION}.deb"

    if wget --spider -q "${API_DEB_URL}" 2>/dev/null; then
        wget -q "${API_DEB_URL}" -O sdrplay_api.deb
        sudo apt-get install -y ./sdrplay_api.deb
        rm -f sdrplay_api.deb
    else
        warn "官网 .deb 链接不存在 (${API_DEB_URL})。"
        warn "改用通用 .run 安装包："
        RUN_URL="https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-${SDRPLAY_API_VERSION}.run"
        if wget --spider -q "${RUN_URL}" 2>/dev/null; then
            wget -q "${RUN_URL}" -O sdrplay_api.run
            chmod +x sdrplay_api.run
            sudo ./sdrplay_api.run
            rm -f sdrplay_api.run
        else
            err "找不到 SDRplay API v${SDRPLAY_API_VERSION} 的下载包。"
            err "请去 https://www.sdrplay.com/downloads/ 手动下载与本机匹配的版本，"
            err "或者把 SDRPLAY_API_VERSION 设成实际可用的版本号再跑："
            err "    SDRPLAY_API_VERSION=3.x.y bash scripts/install_wsl2.sh"
            exit 1
        fi
    fi
fi

# ===== 3. 编译 SoapySDRPlay3 =====
step "编译 SoapySDRPlay3 ..."
SOAPY_SDRPLAY3_DIR="${HOME}/SoapySDRPlay3"
if [ -d "${SOAPY_SDRPLAY3_DIR}" ]; then
    warn "${SOAPY_SDRPLAY3_DIR} 已存在，尝试重新构建 ..."
    cd "${SOAPY_SDRPLAY3_DIR}"
    git pull --rebase || true
else
    cd "${HOME}"
    git clone https://github.com/pothosware/SoapySDRPlay3.git
    cd "${SOAPY_SDRPLAY3_DIR}"
fi

mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -20
make -j"$(nproc)"
sudo make install
sudo ldconfig

# ===== 4. udev 规则（真机 Linux 才有意义，WSL2 可选） =====
step "安装 udev 规则（WSL2 通常不需要，但装上无害） ..."
sudo tee /etc/udev/rules.d/99-sdrplay.rules >/dev/null <<'EOF'
# SDRplay RSP family
SUBSYSTEM=="usb", ATTRS{idVendor}=="1df7", MODE="0666"
EOF
sudo udevadm control --reload-rules 2>/dev/null || true
sudo udevadm trigger 2>/dev/null || true

# ===== 5. 验证 =====
step "验证安装 ..."
echo
echo "--- SoapySDRUtil --info ---"
SoapySDRUtil --info
echo
echo "--- SoapySDRUtil --find ---"
SoapySDRUtil --find || warn "没找到设备。如果还没插 RSP1 / 没 usbipd attach，请先做好硬件侧。"
echo
echo "--- lsusb | grep -i SDRplay ---"
lsusb 2>/dev/null | grep -i sdrplay || warn "USB 总线里看不到 SDRplay。请先做 WSL2 USB 透传 (见 docs/WSL2_USB.md)。"
echo
echo "--- ldconfig 检查 libsdrplay_api ---"
ldconfig -p | grep -i sdrplay || warn "libsdrplay_api 没找到动态链接缓存。试着 sudo ldconfig。"

step "完成！下一步："
echo "  python3 scripts/test_read_iq.py"
echo "  应该看到 'Got N samples, peak=..., RMS=...' 的输出。"
echo
echo "如果失败了，参见 README.md 和 docs/WSL2_USB.md。"