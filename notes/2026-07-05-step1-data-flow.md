# 2026-07-05 — 第一步数据通路打通

## 硬件/驱动
- SDRplay RSP1 (serial=0000000001) 通过 usbipd-win 5.x 透传到 WSL2
- SDRplay API 3.15.2 手动安装 (libsdrplay_api.so + headers 到 /usr/local)
- SoapySDRPlay3 (commit 6cc3131) 编译到本地 prefix: third_party/install/
- sdrplay_apiService daemon 在 root 下运行（提供 USB → shared memory IPC）

## 关键发现
1. **setGainMode 必须在 setGain 之前**：SoapySDRPlay3 在 AGC 开启时静默忽略 setGain
2. **小 buffer 反而稳定**：WSL2 USB 透传下，readStream 一次读 65536 触发 STREAM_ERROR；
   改用 1024 samples/chunk 后稳定。代价是速率只有 ~0.35 MSPS（请求 2 MSPS）
3. **VLF 24 kHz 噪声形态正常**：DC 中心峰 + 1/f 噪声，没有明显干扰（窗口选得不错）

## 当前性能
- 2 MSPS 目标 / 0.35 MSPS 实际 (USB 透传开销 ~6 倍)
- 数据无丢失，无 SOAPY_SDR_STREAM_ERROR

## 待办
- [ ] 接天线（VLF 段需要 1m 鞭状 + 滤波）后跑长时段，看 sferics 脉冲
- [ ] 优化 USB 透传：研究能否调到 ~1 MSPS 实际速率
- [ ] sferics 检测算法（包络+阈值）
