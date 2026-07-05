#!/usr/bin/env python3
"""
raw_capture.py

持续把 RSP1 的 IQ 流写到磁盘，可指定时长或按 Ctrl+C 停止。

输出格式: complex float32 interleaved (.cfile)，能被许多 SDR 工具读
        (inspectrum, GNU Radio, MATLAB 等都吃这个格式)。

用法 (任意平台):
    # 录 30 秒 VLF sferics
    python scripts/raw_capture.py --center-freq 24e3 --duration 30 \
        --output data/vlf_$(Get-Date -Format yyyyMMdd_HHmmss).cfile

    # 不限时长，按 Ctrl+C 停止
    python scripts/raw_capture.py --center-freq 24e3 --output data/vlf_long.cfile

跨平台:
    scripts/_env.py 自动处理 SOAPY_SDR_PLUGIN_PATH.
    假设已激活 conda 环境 'sdr'.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

import _env  # noqa: F401

import numpy as np

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
except ImportError:
    print("缺 SoapySDR Python 绑定。请确认已激活 conda 环境 'sdr':")
    print("    conda activate sdr")
    sys.exit(1)


_stop = False


def _on_sigint(sig, frame):
    global _stop
    print("\n[信息] 收到 SIGINT，停止采集 ...")
    _stop = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--center-freq", type=float, default=24e3)
    parser.add_argument("--sample-rate", type=float, default=2e6)
    parser.add_argument("--bandwidth", type=float, default=0)
    parser.add_argument("--duration", type=float, default=0,
                        help="录多少秒，0 = 无限，按 Ctrl+C 停止")
    parser.add_argument("--output", "-o", required=True, help="输出 .cfile 路径")
    parser.add_argument("--chunk-mb", type=float, default=64,
                        help="每块写盘大小 (MB)，默认 64")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _on_sigint)

    # 找设备
    devs = SoapySDR.Device.enumerate({"driver": "sdrplay"})
    if not devs:
        print("[错误] 没找到 sdrplay 设备")
        sys.exit(1)
    sdr = SoapySDR.Device(devs[0])
    print(f"[信息] 设备: {sdr.getHardwareInfo()}")

    channel = 0
    sdr.setSampleRate(SOAPY_SDR_RX, channel, args.sample_rate)
    sdr.setFrequency(SOAPY_SDR_RX, channel, args.center_freq)
    if args.bandwidth > 0:
        sdr.setBandwidth(SOAPY_SDR_RX, channel, args.bandwidth)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, channel, False)
    except Exception:
        pass

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [channel])
    mtu = sdr.getStreamMTU(stream)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 块大小（以样本数计）
    chunk_samples = int(args.chunk_mb * 1024 * 1024 // 8)  # complex64 = 8 字节
    chunk_samples = (chunk_samples // mtu) * mtu  # 对齐到 MTU

    print(f"[信息] 开始采集: center={args.center_freq/1e3:.1f} kHz, "
          f"sr={args.sample_rate/1e6:.3f} MSPS, mtu={mtu}, "
          f"chunk={chunk_samples} samples")
    print(f"[信息] 输出: {out}")

    sdr.activateStream(stream)

    buf = np.empty(mtu, dtype=np.complex64)
    chunk = np.empty(chunk_samples, dtype=np.complex64)
    chunk_pos = 0
    n_total = 0
    t0 = time.time()

    with open(out, "wb") as f:
        try:
            while not _stop:
                if args.duration > 0 and (time.time() - t0) >= args.duration:
                    print("[信息] 达到指定时长，停止")
                    break
                sr = sdr.readStream(stream, [buf], len(buf))
                if sr.ret <= 0:
                    continue
                seg = buf[: sr.ret]
                left = chunk_samples - chunk_pos
                if sr.ret >= left:
                    chunk[chunk_pos:] = seg[:left]
                    chunk.tofile(f)
                    chunk_pos = 0
                    n_total += chunk_samples
                    # 进度
                    elapsed = time.time() - t0
                    mb = n_total * 8 / 1024 / 1024
                    print(f"\r  {elapsed:7.1f}s, {mb:8.1f} MB, "
                          f"{mb/elapsed:6.2f} MB/s", end="", flush=True)
                else:
                    chunk[chunk_pos : chunk_pos + sr.ret] = seg
                    chunk_pos += sr.ret
        finally:
            # 把残余也写出去
            if chunk_pos > 0:
                chunk[:chunk_pos].tofile(f)
                n_total += chunk_pos
            sdr.deactivateStream(stream)
            sdr.closeStream(stream)
            sdr.unmake()

    elapsed = time.time() - t0
    print()
    print("=" * 60)
    print(f" 总计: {n_total:,} samples = {n_total * 8 / 1024 / 1024:.2f} MB")
    print(f" 时长: {elapsed:.2f} s")
    print(f" 实际速率: {n_total / elapsed / 1e3:.1f} kSPS "
          f"(目标 {args.sample_rate/1e3:.1f} kSPS)")
    print(f" 输出: {out}")
    print("=" * 60)


if __name__ == "__main__":
    main()