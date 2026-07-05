# 调研笔记：SDRplay RSP1 雷电探测

调研时间：2026-07-05

## 1. 硬件：SDRplay RSP1

### 1.1 关键规格（来源：SDRplay 官方 datasheet V3 + 多处核对）

| 项目 | 规格 |
|---|---|
| 频率范围 | **10 kHz – 2 GHz** |
| ADC 位数 | **12-bit native**, <6 MHz 采样时 14-bit, 抽值后可至 16-bit |
| ADC 采样率 | 10.66 MSPS native |
| 实时带宽 | 最高 **10 MHz** |
| 调谐架构 | 直接转换（无上变频） |
| 接口 | USB 2.0 |
| 芯片组 | Mirics |
| 状态 | **已停产**（2017 停产，被 RSP1A 替代，2024 被 RSP1B 替代） |

### 1.2 对雷电探测的关键意义

- ✅ **覆盖 VLF 频段**（3–30 kHz）：闪电辐射的主能量带。RSP1 最低 10 kHz 足够低。
- ✅ **覆盖 LF 频段**（30–300 kHz）：回击信号强，适合远距离接收。
- ✅ **10 MHz 实时带宽**：单次扫描可覆盖整个 VLF + 部分 LF。
- ✅ **14-bit 有效分辨率**（低采样率下）：动态范围足够区分 sferics 脉冲与背景噪声。

### 1.3 劣势

- ❌ **USB 2.0 上限**：单次流传输最高约 8–10 MHz（实测 USB 2.0 high-bandwidth 模式）。
- ❌ **没有 GPS 同步输入**：要做 TOA 多站定位必须外接 GPSDO。
- ❌ **没有原厂雷电探测模式**：得自己写 DSP。

## 2. 软件 API 路径（Linux 上读取 IQ 数据）

### 2.1 SDRplay 官方 API（libsdrplay_api）

- **来源**：<https://www.sdrplay.com/downloads/>，下载 `SDRplay_RSP_API-Linux-x86_xxx.run`
- **暴露**：C API（`mir_sdr_*` 风格的 `sdrplay_api_*`）
- **功能**：底层 IQ 流、调谐、增益、AAGC、宽带/窄带模式
- **缺点**：闭源，只能从官网下载，且 RSP1 / RSP1A / RSP2 / RSPdx 各代 API 略有差异
- **当前 RSP1 兼容版本**：API 3.07 起支持 RSP1（RSP1 已被列入 legacy 列表，但 API 仍可用）

### 2.2 SoapySDR + SoapySDRPlay3（推荐）

- **SoapySDR**：跨 SDR 设备的统一抽象库（Pothosware 维护）
- **SoapySDRPlay3**：SoapySDR 的 SDRplay 后端，调用上面的官方 API
- **优点**：
  - Python / C++ / Julia / GNU Radio 等多语言绑定
  - 一次写代码，将来换 SDR 硬件（RTL-SDR、HackRF、Airspy）改动很小
  - 社区文档丰富
- **安装方式**（WSL2 Ubuntu 24.04）：
  ```bash
  sudo apt install soapysdr-tools libsoapysdr-dev python3-soapysdr
  # SDRplay API 手动装，SoapySDRPlay3 从 github 编译
  ```

### 2.3 apt 包名（已确认可用）

```
soapysdr-tools           # SoapySDRUtil
libsoapysdr0.8           # 运行时
libsoapysdr-dev          # 头文件
python3-soapysdr         # Python 绑定
python3-numpy            # 已装
python3-scipy            # 已装
python3-matplotlib       # 需要装
libusb-1.0-0-dev         # 编译 SoapySDRPlay3 需要
cmake build-essential    # 编译 SoapySDRPlay3 需要
git wget                 # 拉源码/下API
```

注意：`soapysdr-module-mirisdr` 这个包**不能**驱动 RSP1，那是给老式 Mirics 电视棒用的。RSP1 必须走 SDRplay API + SoapySDRPlay3。

## 3. 相关工作（Related Work）

### 3.1 商业/社区网络

| 项目 | 频段 | 硬件 | 规模 |
|---|---|---|---|
| **Blitzortung** | VLF | 自研 + GPS | 全球 3000+ 站，业余+科研，<https://www.blitzortung.org> |
| **Vaisala GLD360** | VLF/HF | 商业 ASR | 全球商用 |
| **Earth Networks Sferic Maps** | VLF | 商业 | 全球商用 |

Blitzortung 是最大、最适合 DIY 复刻的参考对象。

### 3.2 学术/技术参考

- **Cooray, V. (ed.), "The Lightning Flash"** — IEEE 电力工程标准参考书
- **VLF remote sensing** — Stanford VLF Group 历史工作
- **Lightnix** — Linux-based VLF receiver 概念原型（部分博客提到，仓库不一定稳定）
- 多个 **RTL-SDR VLF 改造**博客：把 R820T 电视棒改成 50 kHz 监听，附带简易前放

### 3.3 软件算法

- **sferics detection**：包络检波 + 滑动窗能量阈值（经典实现）
- **TOA 定位**：多个站点检测同一脉冲，比较到达时间差 → 双曲线交点
- **3D 定位**：加磁场方向信息（E-field + H-field 双天线）
- **去噪**：陷波 50/60 Hz 工频、陷波已知导航台（如 NSS / NWC / DHO38 等）

## 4. 频段选择（VLF sferics）

| 频段 | 用途 | 推荐度 |
|---|---|---|
| **3–30 kHz (VLF)** | 闪电天电主能量，远距离传播（地球-电离层波导） | ⭐⭐⭐ 首选 |
| 30–300 kHz (LF) | 强本地回击信号，夜间可远距离 | ⭐⭐ 次选 |
| 3–30 MHz (HF) | 天电经电离层反射的"tweeks"/"whistlers"，有研究价值 | ⭐ 进阶 |

**推荐**：先用 RSP1 调到 **20–24 kHz** 中心频率（避开 VLF 导航台的强干扰，例如中国 BPC 68.5 kHz 导航信号；也避开 NSS 21.4 kHz）。实际找一段干净的窗口段监听。

## 5. 关键风险/注意事项

1. **WSL2 USB 透传**：RSP1 必须在 Windows 侧先 attach 到 WSL2，否则 `/dev/bus/usb` 里看不到。详见 `WSL2_USB.md`。
2. **SDRplay API 版本兼容性**：API 3.x 同时支持老 RSP1 和新 RSP1A/RSPdx，但偶尔有 breaking change。装完后用 `SoapySDRUtil --probe="driver=sdrplay"` 验证。
3. **增益策略**：低频段噪声很大，不要用 AGC（AGC 会把 sferics 脉冲削平）。建议手动设增益，或者只做 coarse AGC + manual IF gain。
4. **天线**：VLF 段的天线设计是个独立课题。简易做法：
   - E-field：1m 鞭状天线 + 9:1 阻抗变换器 + 直流隔离
   - 室外架设，远离 AC 电源
   - 用电池供电的前置放大（避免工频 50 Hz 噪声）

## 6. 待办（随调研深入补充）

- [ ] 找几篇 sferics 实时检测的公开代码仓库
- [ ] 调研 TOA 多站定位的算法细节
- [ ] 调研低成本 GPSDO（用于多站同步）