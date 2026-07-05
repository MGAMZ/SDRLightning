#!/usr/bin/env python3
"""
test_read_iq.py

第一步验证脚本：从 SDRplay RSP1 读取原始 IQ 数据。

用法:
    python scripts/test_read_iq.py
    python scripts/test_read_iq.py --center-freq 24e3 --sample-rate 2e6 --duration 5
    python scripts/test_read_iq.py --record data/test_iq.cfile  # 存到文件

成功标志:
    脚本最后打印 'OK: read NNN samples, peak=XXX, RMS=YYY'
    如果 ZCxx 系列的 RSP1 实时输出,峰值/RMS 不会是 0（背景噪声 + 干扰）

跨平台:
    scripts/_env.py 自动处理 SOAPY_SDR_PLUGIN_PATH.
    假设已激活 conda 环境 'sdr' (conda activate sdr).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import _env  # noqa: F401

import numpy as np

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
except ImportError:
    print("找不到 SoapySDR Python 绑定。请先：")
    print("    conda activate sdr   (Windows / Linux)")
    print("然后再执行本脚本。")
    sys.exit(1)


def enumerate_devices():
    """列出所有 SDRplay 设备"""
    results = SoapySDR.Device.enumerate({"driver": "sdrplay"})
    if not results:
        # 兜底：列出所有 SDR
        print("[警告] driver=sdrplay 没找到。列出所有设备：")
        all_devs = SoapySDR.Device.enumerate()
        for d in all_devs:
            print(" ", d)
        if not all_devs:
            print("  (没有任何 SDR 设备)")
        print()
        print("可能的原因：")
        print("  1. RSP1 还没透传到 WSL2（看 docs/WSL2_USB.md）")
        print("  2. SDRplay API 或 SoapySDRPlay3 没装好")
        print("  3. USB 线松了")
        sys.exit(1)
    return results


def find_rsp1(results):
    """挑一个 RSP1 设备返回"""
    for r in results:
        # SoapySDRKwargs 不是普通 dict；用 at(key) 取值
        try:
            driver = r.at("driver").lower() if r.has("driver") else ""
        except Exception:
            driver = ""
        if "sdrplay" in driver:
            return r
    return results[0]


def open_stream(args):
    """打开 SDRplay 设备并配置成收 IQ 流"""
    devs = enumerate_devices()
    args_dict = find_rsp1(devs)
    print(f"[信息] 使用设备: {args_dict}")

    sdr = SoapySDR.Device(args_dict)

    # --- 显示基本信息 ---
    hw_info = sdr.getHardwareInfo()
    if hw_info:
        print(f"[信息] 硬件信息: {hw_info}")
    else:
        try:
            print(f"[信息] 硬件 key=value: {hw_info.keys()}")
        except Exception:
            print(f"[信息] 硬件信息对象: {hw_info}")

    # --- 配置通道 ---
    channel = 0
    sdr.setSampleRate(SOAPY_SDR_RX, channel, args.sample_rate)
    sdr.setFrequency(SOAPY_SDR_RX, channel, args.center_freq)

    # 关键：先关 AGC，再设具体增益（顺序反了 SoapySDRPlay3 会警告并忽略 setGain）
    try:
        sdr.setGainMode(SOAPY_SDR_RX, channel, False)  # manual
        print("[信息] 已切换到手动增益（关 AGC）")
    except Exception as e:
        print(f"[警告] setGainMode 失败: {e}")

    # 现在再设具体增益元素
    try:
        gains = sdr.listGains(SOAPY_SDR_RX, channel)
        print(f"[信息] 可调增益元素: {gains}")
        for g in gains:
            try:
                rng = sdr.getGainRange(SOAPY_SDR_RX, channel, g)
                # 取中点
                mid = (rng.minimum() + rng.maximum()) / 2
                sdr.setGain(SOAPY_SDR_RX, channel, g, mid)
                print(f"  设 {g} = {mid:.1f} dB (范围 {rng.minimum():.1f}..{rng.maximum():.1f})")
            except Exception as e:
                print(f"  设 {g} 失败: {e}")
    except Exception as e:
        print(f"[警告] 列举/设置 gain 失败: {e}")

    # --- 配置流 ---
    if args.bandwidth > 0:
        sdr.setBandwidth(SOAPY_SDR_RX, channel, args.bandwidth)

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [channel])
    print(f"[信息] 流已创建: MTU={sdr.getStreamMTU(stream)} samples")
    time.sleep(0.2)  # 让 SoapySDRPlay3 完成内部初始化

    return sdr, stream


def read_samples(sdr, stream, duration_s, sample_rate, mtu):
    """读 duration 秒的 IQ 数据，返回 numpy complex64 数组"""
    n_total = int(sample_rate * duration_s)
    n_read = 0
    # 每次读 1024 个样本（SoapySDRPlay3 在 WSL2 上读大块会出 SOAPY_SDR_STREAM_ERROR）
    chunk = 1024
    buf = np.empty(chunk, dtype=np.complex64)
    chunks = []

    sdr.activateStream(stream)
    t0 = time.time()
    t_last_print = t0
    try:
        while n_read < n_total:
            sr = sdr.readStream(stream, [buf], chunk, timeoutUs=1000000)
            if sr.ret < 0:
                print(f"[错误] readStream 返回 {sr.ret}")
                break
            if sr.ret > 0:
                chunks.append(buf[: sr.ret].copy())
                n_read += sr.ret
                t_now = time.time()
                if t_now - t_last_print >= 0.5:
                    rate = n_read / (t_now - t0) if t_now > t0 else 0
                    print(f"  已读 {n_read / 1e6:.2f} MSamples, 实际速率 {rate / 1e6:.2f} MSPS")
                    t_last_print = t_now
    finally:
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)

    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.complex64)


def stats_and_save(iq, args):
    """统计 IQ 数据，可选存到文件"""
    n = len(iq)
    if n == 0:
        print("[错误] 没读到任何样本")
        return

    peak = float(np.max(np.abs(iq)))
    rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
    mean_i = float(np.mean(iq.real))
    mean_q = float(np.mean(iq.imag))

    print()
    print("=" * 60)
    print(f" 读取样本数:  {n:,}")
    print(f" 实际时长:    {n / args.sample_rate:.3f} s")
    print(f" Peak |IQ|:   {peak:.4f}  (1.0 = 满量程)")
    print(f" RMS |IQ|:    {rms:.4f}")
    print(f" Crest:       {peak / rms:.2f}×  (>5 说明有脉冲)")
    print(f" I 均值:      {mean_i:+.4f}")
    print(f" Q 均值:      {mean_q:+.4f}")
    print("=" * 60)

    # 简单判定
    if rms < 1e-6:
        print("[警告] RMS 几乎为 0——可能没信号或硬件没真正输出")
        print("       检查天线/连接；也可能是 SDR 没真正 active。")
    elif peak > 0.95:
        print("[警告] 接近满量程，可能过载。考虑降低增益。")
    else:
        print("[OK] 看起来读到的是真实信号。")
        print(f"     第一步验证通过。")

    # 可选：FFT 快速看一下频谱
    try:
        import matplotlib
        matplotlib.use("Agg")  # 非交互后端
        import matplotlib.pyplot as plt
        from scipy import signal as sps

        # 取前 2^18 个样本做 FFT
        n_fft = min(2 ** 18, n)
        seg = iq[:n_fft] * np.hanning(n_fft)
        spec = np.fft.fftshift(np.fft.fft(seg))
        freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1.0 / args.sample_rate))
        psd_db = 20 * np.log10(np.abs(spec) + 1e-12)

        out_png = Path(args.fft_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(freqs / 1e3, psd_db, lw=0.5)
        ax.set_xlabel("Frequency relative to center (kHz)")
        ax.set_ylabel("Magnitude (dB)")
        ax.set_title(
            f"RSP1 接收频谱 — center={args.center_freq/1e3:.1f} kHz, "
            f"sr={args.sample_rate/1e6:.2f} MSPS"
        )
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_png, dpi=110)
        print(f"[信息] 频谱图已存到 {out_png}")
    except Exception as e:
        print(f"[警告] FFT/画图失败: {e}")

    # 可选：存到 .cfile (complex float32, interleaved IQ)
    if args.record:
        out = Path(args.record)
        out.parent.mkdir(parents=True, exist_ok=True)
        iq.tofile(out)
        size_mb = out.stat().st_size / 1024 / 1024
        print(f"[信息] IQ 已存到 {out} ({size_mb:.2f} MB, complex float32)")
        print(f"       可用 `python3 -c \"import numpy as np; "
              f"x=np.fromfile('{out}', dtype=np.complex64); print(x.shape)\"` 读回。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--center-freq", type=float, default=24e3,
        help="中心频率 (Hz)，默认 24 kHz（VLF sferics 干净窗口）"
    )
    parser.add_argument(
        "--sample-rate", type=float, default=2e6,
        help="采样率 (Hz)，默认 2 MSPS（够用，USB 2.0 友好）"
    )
    parser.add_argument(
        "--bandwidth", type=float, default=0,
        help="模拟带宽 (Hz)，0 = 用 sample_rate 默认"
    )
    parser.add_argument(
        "--duration", type=float, default=2.0,
        help="读多少秒，默认 2 秒（够看到噪声基底）"
    )
    parser.add_argument(
        "--record", type=str, default=None,
        help="保存 IQ 到文件 (.cfile, complex float32 interleaved)"
    )
    parser.add_argument(
        "--fft-png", type=str, default="data/test_fft.png",
        help="频谱图 PNG 输出路径"
    )
    args = parser.parse_args()

    print("=" * 60)
    print(" SDRplay RSP1 第一步验证 — 读取原始 IQ 数据")
    print("=" * 60)
    print(f" 中心频率: {args.center_freq / 1e3:.1f} kHz")
    print(f" 采样率:   {args.sample_rate / 1e6:.3f} MSPS")
    print(f" 时长:     {args.duration:.2f} s")
    print(f" 模拟带宽: {args.bandwidth} Hz (0=自适应)")
    print()

    sdr, stream = open_stream(args)
    mtu = sdr.getStreamMTU(stream)

    try:
        iq = read_samples(sdr, stream, args.duration, args.sample_rate, mtu)
    finally:
        try:
            sdr.unmake()
        except Exception:
            pass

    stats_and_save(iq, args)

    # 最终退出码
    if len(iq) == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()