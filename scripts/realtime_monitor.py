#!/usr/bin/env python3
"""
realtime_monitor.py

实时从 RSP1 读 IQ，**持续输出到 stdout**：
  - 每 N 秒打印一行 [SPEC] 最新频谱统计 + ASCII bar
  - 检测到 flash 时打印 [FLASH] 一行 (T_w 窗积分功率超 baseline Δ dB)

不依赖 GUI / TTY。可以：
    python scripts/realtime_monitor.py | Tee-Object -FilePath sdr.log
也可以加 --plot 用 plotext 在 TTY 里画（如果有 X / 终端支持）。

用法 (任意平台):
    python scripts/realtime_monitor.py
    python scripts/realtime_monitor.py --center-freq 24e3 --sample-rate 2e6 --duration 60

Linux/WSL 老用法兼容:
    source scripts/env.sh
    python3 scripts/realtime_monitor.py

跨平台:
    scripts/_env.py 自动处理 SOAPY_SDR_PLUGIN_PATH.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections import deque

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
    print("\n[信息] 收到 SIGINT，停止 ...")
    _stop = True


def ascii_bar(value, width=40, vmin=-60, vmax=0):
    """把 dB 值映射到 ASCII bar"""
    v = np.clip(value, vmin, vmax)
    n = int((v - vmin) / (vmax - vmin) * width)
    return "█" * n + "·" * (width - n)


def open_sdr(args):
    devs = SoapySDR.Device.enumerate({"driver": "sdrplay"})
    if not devs:
        print("[错误] 没找到 sdrplay 设备")
        sys.exit(1)
    sdr = SoapySDR.Device(devs[0])
    print(f"[+] 设备: {sdr.getHardwareInfo()}")

    sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    for g in sdr.listGains(SOAPY_SDR_RX, 0):
        rng = sdr.getGainRange(SOAPY_SDR_RX, 0, g)
        sdr.setGain(SOAPY_SDR_RX, 0, g, (rng.minimum() + rng.maximum()) / 2)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.sample_rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, args.center_freq)
    if args.bandwidth > 0:
        sdr.setBandwidth(SOAPY_SDR_RX, 0, args.bandwidth)

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0])
    return sdr, stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--center-freq", type=float, default=24e3)
    parser.add_argument("--sample-rate", type=float, default=2e6)
    parser.add_argument("--bandwidth", type=float, default=0)
    parser.add_argument("--fft-size", type=int, default=2048)
    parser.add_argument("--chunk", type=int, default=1024)
    parser.add_argument("--print-period", type=float, default=1.0,
                        help="每隔多少秒打印一帧 [SPEC]")
    parser.add_argument("--flash-window-ms", type=float, default=200.0,
                        help="窗长 T_w: 一次强度积分的时间 (ms)")
    parser.add_argument("--flash-baseline-ms", type=float, default=1000.0,
                        help="baseline 回看时长 T_b (ms)")
    parser.add_argument("--flash-thresh-db", type=float, default=12.0,
                        help="窗功率超 baseline 多少 dB 触发")
    parser.add_argument("--flash-cooldown-ms", type=float, default=1000.0,
                        help="两次 flash 之间冷却 (ms)")
    parser.add_argument("--duration", type=float, default=0,
                        help="运行时长 (0=无限)")
    parser.add_argument("--plot", action="store_true",
                        help="额外用 plotext 在 TTY 里画图")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _on_sigint)

    if args.plot:
        try:
            import plotext as plt  # noqa: F401
        except ImportError:
            print("[警告] plotext 没装，--plot 忽略")
            args.plot = False

    print(f"[+] 配置: center={args.center_freq/1e3:.1f} kHz, "
          f"sr={args.sample_rate/1e6:.2f} MSPS, fft={args.fft_size}, "
          f"chunk={args.chunk}")
    sdr, stream = open_sdr(args)
    sdr.activateStream(stream)
    print("[+] 开始实时监控 (Ctrl+C 停止)")

    buf = np.empty(args.chunk, dtype=np.complex64)
    fft_buf = np.zeros(args.fft_size, dtype=np.complex64)
    fft_n = 0
    env_window = deque(maxlen=400)
    intensity_ring = deque(maxlen=max(2, int(args.flash_baseline_ms / args.flash_window_ms) + 4))
    win_target_n = max(1, int(args.sample_rate * args.flash_window_ms / 1000))
    win_acc_power = 0.0
    win_acc_n = 0
    flash_count = 0
    last_trigger_t = -1e9

    t_start = time.time()
    n_total = 0
    next_print = t_start + args.print_period

    try:
        while not _stop:
            if args.duration > 0 and (time.time() - t_start) > args.duration:
                break
            sr = sdr.readStream(stream, [buf], args.chunk, timeoutUs=1000000)
            if sr.ret <= 0:
                continue
            seg = buf[: sr.ret]
            n_total += sr.ret

            # 包络 (供 UI 显示 / baseline 监视)
            env_window.append(float(np.mean(np.abs(seg))))
            env_db_now = 20 * np.log10(env_window[-1] + 1e-12)
            env_db_mean = 20 * np.log10(np.mean(env_window) + 1e-12)

            # === flash 检测: 窗积分功率 vs baseline ===
            t_now = time.time()
            win_acc_power += float(np.sum(np.abs(seg) ** 2))
            win_acc_n += sr.ret
            if win_acc_n >= win_target_n:
                i_k = win_acc_power / win_acc_n
                i_k_db = 10 * np.log10(i_k + 1e-18)
                intensity_ring.append(i_k_db)
                baseline_db = float(np.mean(intensity_ring)) if intensity_ring else -100.0
                excess_db = i_k_db - baseline_db

                in_cd = (t_now - last_trigger_t) * 1000 < args.flash_cooldown_ms
                if (not in_cd) and excess_db > args.flash_thresh_db:
                    last_trigger_t = t_now
                    flash_count += 1
                    seg_peak = float(np.max(np.abs(seg)))
                    print(f"[FLASH #{flash_count:04d}] t={t_now-t_start:7.2f}s  "
                          f"window_peak={seg_peak:.3f}  excess=+{excess_db:.1f} dB "
                          f"(baseline={baseline_db:.1f} dB, T_w={args.flash_window_ms:.0f}ms)",
                          flush=True)
                win_acc_power = 0.0
                win_acc_n = 0

            # 累积 FFT（chunk <= fft_size 时一帧填不满，不算；下一个 chunk 续写）
            n = len(seg)
            while n > 0:
                space = args.fft_size - fft_n
                take = min(space, n)
                fft_buf[fft_n : fft_n + take] = seg[:take]
                fft_n += take
                seg = seg[take:]
                n -= take

            # 周期打印时算 FFT
            if t_now >= next_print:
                win = np.hanning(args.fft_size)
                spec = np.fft.fftshift(np.fft.fft(fft_buf * win))
                psd_db = 20 * np.log10(np.abs(spec) + 1e-12)
                freqs = np.fft.fftshift(np.fft.fftfreq(args.fft_size, 1.0 / args.sample_rate)) / 1e3

                # 降采样到 40 个 bin 画 ASCII
                bins = 40
                edges = np.linspace(0, len(psd_db), bins + 1).astype(int)
                ds = np.array([np.mean(psd_db[edges[i]:edges[i+1]]) for i in range(bins)])
                ds -= ds.max()  # 归一化

                bar_width = 30
                print(f"[SPEC] t={t_now-t_start:6.1f}s  samples={n_total:,}  "
                      f"env_mean={env_db_mean:+5.1f} dB  env_now={env_db_now:+5.1f} dB  "
                      f"flashes={flash_count}")
                print(f"       fc={args.center_freq/1e3:7.2f} kHz, "
                      f"span=±{args.sample_rate/2/1e3:.0f} kHz")
                for i, v in enumerate(ds):
                    f_lo = freqs[edges[i]]
                    f_hi = freqs[edges[i+1]-1]
                    print(f"  {f_lo:+6.1f}..{f_hi:+6.1f} kHz  "
                          f"{ascii_bar(v, bar_width):<{bar_width}}  {v:+5.1f} dB")
                print("", flush=True)
                next_print = t_now + args.print_period
    finally:
        try:
            sdr.deactivateStream(stream)
            sdr.closeStream(stream)
            sdr.unmake()
        except Exception:
            pass

    print(f"\n[+] 结束. 总采样 {n_total:,} samples, "
          f"运行时长 {time.time()-t_start:.1f}s, "
          f"flashes 检测到 {flash_count} 次")


if __name__ == "__main__":
    main()