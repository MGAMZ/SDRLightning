# SDRplay RSP1 Lightning Detector

把 SDRplay RSP1 软件无线电接收机改作雷电探测用途。本仓库第一阶段：验证能否顺利从硬件中读出原始 IQ 数据。

## 项目定位

- **目标硬件**：SDRplay RSP1（已停产，被 RSP1A / RSP1B 取代，但功能完整可用）
- **目标探测**：VLF sferics（3 kHz–30 kHz 频段被动监听闪电天电干扰）
- **运行平台**：Windows (native) **或** WSL2 (Ubuntu 24.04)
- **现状**：第一步——验证 IQ 数据通路

## 目录结构

```
SDR/
├── README.md                       本文件：项目入口
├── AGENTS.md                       AI 协作者上下文（含跨平台踩坑笔记）
├── docs/
│   ├── RESEARCH.md                 硬件/协议/相关工作调研笔记
│   ├── WSL2_USB.md                 usbipd-win 透传 RSP1 到 WSL2 的步骤
│   └── WINDOWS.md                  Windows native 安装与使用
├── scripts/
│   ├── _env.py                     (跨平台) 自动配置 SOAPY_SDR_PLUGIN_PATH
│   ├── env.sh                      (Linux only) 老式环境设置脚本
│   ├── env.ps1                     (Windows) 环境检查 + service 管理
│   ├── install_wsl2.sh             (Linux only) 一键安装
│   ├── start_sdrplay_service.sh    (Linux only) 启动 sdrplay_apiService daemon
│   ├── test_read_iq.py             第一步验证：枚举设备→开流→读IQ→统计
│   ├── minimal_probe.py            最小化探测脚本（调试用）
│   ├── raw_capture.py              持续采集IQ到.cfile
│   ├── realtime_monitor.py         终端实时监控（ASCII 频谱 + sferics）
│   ├── web_monitor.py              单进程 Web 可视化 ⭐ 推荐
│   └── web_monitor/
│       ├── templates/index.html    前端页面
│       └── static/                 CSS / JS / 瀑布图导出
├── third_party/
│   ├── sdrplay_api/                SDRplay API Linux 安装包 + 提取 (legacy)
│   ├── SoapySDRPlay3/              SoapySDRPlay3 源码 + build
│   └── install/                    本地安装前缀 (lib/SoapySDR/modules0.8/)
├── data/                           录制数据（已gitignore）
└── notes/                          临时笔记
```

## 快速开始

### 0. 装 conda 环境（两个平台相同）

```bash
conda create -n sdr python=3.12 -y
conda activate sdr
conda install -c conda-forge --solver=classic \
    soapysdr numpy scipy matplotlib flask flask-socketio plotext -y
```

> ⚠ Windows 上 libmamba solver 偶尔有 DLL 加载问题，加 `--solver=classic` 绕开。

### 1a. Windows（推荐，直接插 USB）

详见 [`docs/WINDOWS.md`](docs/WINDOWS.md)。简版：

1. 装 SDRplay API Windows MSI（从 sdrplay.com 下载）
2. 装 PothosSDR 或自己编译 SoapySDRPlay3
3. 跑 `python scripts\web_monitor.py`

### 1b. WSL2（legacy，需要 usbipd-win 透传）

详见 [`docs/WSL2_USB.md`](docs/WSL2_USB.md)。简版：

```bash
# Windows PowerShell (管理员) 一次性
usbipd bind --busid <X>
usbipd attach --wsl --busid <X>

# WSL2 内
sudo apt install libsoapysdr0.8 libsoapysdr-dev python3-soapysdr libusb-1.0-0-dev cmake
# 编译 SDRplay API + SoapySDRPlay3 到 third_party/install/
sudo nohup bash scripts/start_sdrplay_service.sh > /tmp/sdrplay_service.log 2>&1 &
disown
```

### 2. 跑 Web 可视化（推荐 ⭐）

Windows：
```powershell
conda activate sdr
python scripts\web_monitor.py --port 5000
```

Linux/WSL2：
```bash
conda activate sdr
python3 scripts/web_monitor.py --port 5000
```

浏览器打开 **http://localhost:5000**。

Web UI 功能：
- 实时瀑布图（最近 200 行 FFT，10fps）
- 当前频谱曲线
- 实时包络曲线
- sferics 事件日志（带闪动高亮）
- 控件面板：中心频率 / 采样率 / IFGR / RFGR / sferic 阈值
- 4 个预设频点按钮（24 kHz VLF / 77 kHz DCF77 / 198 kHz BBC / 100 MHz FM）
- 导出瀑布图 PNG

## 当前阶段成果

| 里程碑 | 状态 |
|---|---|
| 调研完成（硬件/协议/相关工作） | ✅ |
| WSL2 USB 透传方案明确 | ✅ |
| Windows native 支持 | ✅ 2026-07-05 |
| 安装脚本就绪 | ✅ |
| **从硬件读到 IQ 数据** | ✅ 2026-07-05 验证通过 (WSL2) |
| 终端实时监控（ASCII） | ✅ |
| **Web 可视化（Flask+WS）** | ✅ |
| VLF sferics 真实检测 | 🔜（等真雷暴 + 调天线增益）|

## 下一步

1. 在浏览器里玩 Web UI，验证瀑布图/频谱/包络显示
2. 调增益（VLF 段建议 RFGR=3 满格 + IFGR ≈ 50）
3. 等真雷暴 → 看到 sferics 实时触发
4. 多站定位（可选）：TOA 需要 GPS 同步

## 参考资料

- SDRplay RSP1 datasheet: <https://www.sdrplay.com/wp-content/uploads/2017/01/161129RSP1DatasheetV3.pdf>
- SoapySDRPlay3: <https://github.com/pothosware/SoapySDRPlay3>
- SDRplay API: <https://www.sdrplay.com/downloads/>
- PothosSDR (Windows): <https://github.com/pothosware/PothosSDR>
- Blitzortung network: <https://www.blitzortung.org/>
- usbipd-win: <https://github.com/dorssel/usbipd-win>

## 许可证

仅作研究/学习用途。