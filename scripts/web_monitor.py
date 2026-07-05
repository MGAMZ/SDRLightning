#!/usr/bin/env python3
"""
web_monitor.py

单进程 Web 可视化 RSP1 雷电探测。

启动 (任意平台, 假设已激活 conda env `sdr`):
    python scripts/web_monitor.py --port 50000

打开浏览器:
    http://localhost:5000
    (WSL2 下 Windows 浏览器直接访问 localhost 即可，WSL2 自动转发)

Linux/WSL 老用法仍然兼容:
    source scripts/env.sh
    python3 scripts/web_monitor.py --port 5000

特性:
    - 实时频谱瀑布 (200 行滚动)
    - 当前频谱曲线
    - 实时包络 (时域)
    - flash (闪电放电) 事件日志 (T_w 窗积分功率 vs baseline)
    - 控件: 中心频率 / 采样率 / 增益 / flash 检测阈值 (T_w/T_b/T_pre/T_post)

跨平台:
    - 跨平台 env 由 scripts/_env.py 自动处理 (SOAPY_SDR_PLUGIN_PATH 等)
    - 显式设了 --host 0.0.0.0 后可被局域网访问
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import deque
from pathlib import Path

import _env  # noqa: F401  (必须最先 import, 见 _env.py docstring)

import numpy as np
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
except ImportError:
    print("缺 SoapySDR Python 绑定。请确认已激活 conda 环境 'sdr':")
    print("    conda activate sdr")
    raise SystemExit(1)


# ============== 全局状态 ==============
APP_DIR = Path(__file__).parent / "web_monitor"

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)
app.config["SECRET_KEY"] = "rsp1-lightning"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


class SDRState:
    """可热改的配置"""

    def __init__(self):
        self.center_freq = 50e6
        self.sample_rate = 10e6   # 默认 10 MSPS（用户偏好）
        self.bandwidth = 0
        self.ifgr = 39
        self.rfgr = 1
        self.fft_size = 1024
        self.chunk = 1024
        # flash 检测参数 (见 reader_loop 注释 / AGENTS.md 定义)
        self.flash_window_ms = 100       # T_w: 一次强度积分的窗长
        self.flash_baseline_ms = 1000    # T_b: baseline 回看时长 (ms)
        self.flash_thresh_db = 12.0      # Δ: 当前窗超 baseline 多少 dB 触发
        self.flash_pre_ms = 500          # T_pre: 触发前保存的 IQ/包络长度
        self.flash_post_ms = 700         # T_post: 触发后继续保存的长度
        self.flash_cooldown_ms = 1000    # T_cd: 抑制同 flash 重复触发 (ms)
        self.flash_env_rate_hz = 1000    # f_env: flash 包络降采样率
        # 实时包络可视化参数 (跟 flash 检测分离, 用于浏览器 UI)
        self.env_window_s = 10.0         # 实时包络图显示多长时间历史
        self.env_rate_hz = 100           # 实时包络的降采样率
        self.lock = threading.Lock()

    def to_dict(self):
        with self.lock:
            return {
                "center_freq": self.center_freq,
                "sample_rate": self.sample_rate,
                "bandwidth": self.bandwidth,
                "ifgr": self.ifgr,
                "rfgr": self.rfgr,
                "fft_size": self.fft_size,
                "chunk": self.chunk,
                "flash_window_ms": self.flash_window_ms,
                "flash_baseline_ms": self.flash_baseline_ms,
                "flash_thresh_db": self.flash_thresh_db,
                "flash_pre_ms": self.flash_pre_ms,
                "flash_post_ms": self.flash_post_ms,
                "flash_cooldown_ms": self.flash_cooldown_ms,
                "flash_env_rate_hz": self.flash_env_rate_hz,
                "env_window_s": self.env_window_s,
                "env_rate_hz": self.env_rate_hz,
            }


state = SDRState()

# SDR 句柄（被 reader 线程使用；主线程在收到 HTTP 控制时改配置）
_sdr = None
_stream = None
_sdr_lock = threading.Lock()
_stream_lock = threading.Lock()

_reader_thread = None
_stop_event = threading.Event()
_running = {"ok": False, "msg": "starting"}

# 数据缓冲（推送和缓存分离）
_waterfall = deque(maxlen=200)  # 每行 = spectrum 数组 (dB)
_envelope = deque(maxlen=600)   # 时域包络 (给浏览器画)
_flashes = deque(maxlen=200)    # 最近检测到的 flash 事件
_flash_count = 0
_total_samples = 0
_t_start = 0.0

# 滚动 IQ 缓冲：容量按 T_pre × sample_rate 计算；sample_rate 调整时重建
def _new_iq_buffer(capacity):
    return np.zeros(capacity, dtype=np.complex64), 0, 0

_iq_ring, _iq_ring_n, _iq_ring_pos = _new_iq_buffer(1)
_iq_ring_capacity = 1
def _resize_iq_buffer(new_cap):
    global _iq_ring, _iq_ring_n, _iq_ring_pos, _iq_ring_capacity
    new_cap = max(1, int(new_cap))
    _iq_ring = np.zeros(new_cap, dtype=np.complex64)
    _iq_ring_n = 0
    _iq_ring_pos = 0
    _iq_ring_capacity = new_cap

# 包络降采样环 (T_pre × f_env 点) 和窗强度环 (T_b / T_w 点)
_env_ring = deque(maxlen=1)
_intensity_ring = deque(maxlen=1)

# 实时显示用的包络环 (固定 env_rate_hz, 容量 = env_window_s × env_rate_hz)
_display_env_ring = deque(maxlen=1)
_display_env_acc = 0.0
_display_env_acc_n = 0
_display_env_last_t = 0.0

# 有效采样率估计 (1 秒滑动窗口, EMA 平滑)
_eff_rate = 0.0
_eff_rate_acc_samples = 0
_eff_rate_last_t = 0.0

def _current_eff_rate():
    """实际从 readStream 拿到的 sample/s, 用于时间→样本换算.
    前 1 秒还没收敛时退回 state.sample_rate."""
    return _eff_rate if _eff_rate > 0 else state.sample_rate

# flash 详情缓存: id -> dict (envelope_trace, spectrum, iq_peak_slice 等)
_flash_details = {}
_MAX_FLASH_CACHE = 50


def push_iq_ring(seg: np.ndarray):
    """把新 samples 写入环形缓冲 (容量跟 sample_rate × T_pre 走)"""
    global _iq_ring_n, _iq_ring_pos
    n = len(seg)
    if n == 0:
        return
    cap = _iq_ring_capacity
    if n >= cap:
        seg = seg[-cap:]
        n = cap
    end = _iq_ring_pos + n
    if end <= cap:
        _iq_ring[_iq_ring_pos:end] = seg
    else:
        first = cap - _iq_ring_pos
        _iq_ring[_iq_ring_pos:] = seg[:first]
        _iq_ring[:n - first] = seg[first:]
    _iq_ring_pos = (_iq_ring_pos + n) % cap
    _iq_ring_n = min(_iq_ring_n + n, cap)


def snapshot_iq_ring() -> np.ndarray:
    """返回当前环形缓冲（按时间顺序，最旧到最新）"""
    if _iq_ring_n < _iq_ring_capacity:
        return _iq_ring[:_iq_ring_n].copy()
    return np.concatenate([_iq_ring[_iq_ring_pos:], _iq_ring[:_iq_ring_pos]]).copy()


# ============== SDR 控制 ==============
def open_sdr():
    """打开/重开 RSP1 流"""
    global _sdr, _stream
    with _sdr_lock, _stream_lock:
        close_sdr_unlocked()
        devs = SoapySDR.Device.enumerate({"driver": "sdrplay"})
        if not devs:
            raise RuntimeError("没找到 sdrplay 设备")
        _sdr = SoapySDR.Device(devs[0])
        print(f"[+] SDR 打开: {_sdr.getHardwareInfo()}")
        _sdr.setSampleRate(SOAPY_SDR_RX, 0, state.sample_rate)
        _sdr.setFrequency(SOAPY_SDR_RX, 0, state.center_freq)
        _sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        _sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", state.ifgr)
        _sdr.setGain(SOAPY_SDR_RX, 0, "RFGR", state.rfgr)
        if state.bandwidth > 0:
            _sdr.setBandwidth(SOAPY_SDR_RX, 0, state.bandwidth)
        _stream = _sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0])
        _sdr.activateStream(_stream)


def close_sdr_unlocked():
    global _sdr, _stream
    if _stream is not None:
        try:
            _sdr.deactivateStream(_stream)
        except Exception:
            pass
        try:
            _sdr.closeStream(_stream)
        except Exception:
            pass
        _stream = None
    if _sdr is not None:
        try:
            _sdr.unmake()
        except Exception:
            pass
        _sdr = None


def apply_config_to_sdr():
    """把当前 state 同步到 SDR（不重建流）"""
    with _sdr_lock:
        if _sdr is None:
            return False
        try:
            _sdr.setFrequency(SOAPY_SDR_RX, 0, state.center_freq)
            _sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", state.ifgr)
            _sdr.setGain(SOAPY_SDR_RX, 0, "RFGR", state.rfgr)
        except Exception as e:
            print(f"[!] apply_config 失败: {e}")
            return False
    return True


def reopen_sdr():
    """采样率变了需要重建流"""
    try:
        open_sdr()
        return True
    except Exception as e:
        _running["ok"] = False
        _running["msg"] = f"open_sdr 失败: {e}"
        print(f"[x] reopen: {e}")
        return False


def retry_open_sdr_loop():
    """后台重试打开 SDR（设备可能被其他进程占用）"""
    while not _stop_event.is_set():
        if _sdr is None and not _running["ok"]:
            try:
                open_sdr()
                _running["ok"] = True
                _running["msg"] = "ok (重连成功)"
                # 启动 reader
                global _reader_thread
                if _reader_thread is None or not _reader_thread.is_alive():
                    _reader_thread = threading.Thread(target=reader_loop, daemon=True)
                    _reader_thread.start()
            except Exception as e:
                _running["msg"] = f"等待设备: {e}"
        time.sleep(2)


# ============== Reader 线程 ==============
DISPLAY_BINS = 256          # 推送到前端的频谱 bin 数（下采样）
FRAME_INTERVAL_S = 0.066    # 推送间隔 (~15 fps)
OVERFLOW_THRESHOLD = 0.95   # |IQ| > 此值视为过载


def decimate_maxpool(spec: np.ndarray, n_out: int) -> np.ndarray:
    """max-pooling 下采样：保留每个块内的峰值"""
    n_in = len(spec)
    if n_in == n_out:
        return spec
    edges = np.linspace(0, n_in, n_out + 1).astype(int)
    edges[-1] = n_in
    return np.maximum.reduceat(spec, edges[:-1])


def _downsample_envelope_chunk(seg: np.ndarray, samples_per_out: float) -> list:
    """把 seg 的 |seg| 用 max-pool 降采样, 返回 list[float]"""
    abs_seg = np.abs(seg)
    n = len(abs_seg)
    n_out = max(0, int(n / samples_per_out))
    if n_out == 0:
        return []
    edges = np.linspace(0, n, n_out + 1).astype(int)
    edges[-1] = n
    return [float(np.max(abs_seg[e0:e1])) for e0, e1 in zip(edges[:-1], edges[1:])]


def _rebuild_buffers_for_state():
    """按当前 state 参数初始化 ring/deque 容量 (sample_rate/env_window_s 等变化后调用)"""
    global _env_ring, _intensity_ring, _display_env_ring
    env_cap = max(2, int(state.flash_env_rate_hz * state.flash_pre_ms / 1000) + 64)
    _env_ring = deque(maxlen=env_cap)
    int_cap = max(2, int(state.flash_baseline_ms / state.flash_window_ms) + 4)
    _intensity_ring = deque(maxlen=int_cap)
    disp_cap = max(50, int(state.env_window_s * state.env_rate_hz) + 20)
    _display_env_ring = deque(maxlen=disp_cap)
    iq_cap = max(1, int(state.sample_rate * state.flash_pre_ms / 1000))
    _resize_iq_buffer(iq_cap)


def _finalize_flash(post_capture: dict):
    """post 阶段结束, 拼装 flash 事件 payload, 推送给前端, 缓存 IQ peak slice"""
    ev = post_capture["ev"]
    pre_env = ev["pre_env"]
    post_env = post_capture["post_env"]
    env_rate = ev["env_rate_hz"]
    sample_rate = ev["sample_rate_hz"]   # 声明值, 写在 payload 里给 UI 展示
    pre_ms = ev["pre_ms"]
    sr_eff = _current_eff_rate()         # 实际值, 用来定位 IQ 样本

    envelope_trace = np.array(pre_env + post_env, dtype=np.float32)
    pre_iq = snapshot_iq_ring()
    if post_capture["post_iq_chunks"]:
        post_iq = np.concatenate(post_capture["post_iq_chunks"])
    else:
        post_iq = np.zeros(0, dtype=np.complex64)

    # peak index in envelope, 换算到 captured IQ 里的 sample 位置 (用有效采样率)
    if envelope_trace.size > 0:
        peak_idx = int(np.argmax(envelope_trace))
        peak_t_s = peak_idx / env_rate
    else:
        peak_idx = 0
        peak_t_s = 0.0
    pre_n_samples = int(pre_ms / 1000.0 * sr_eff)
    peak_iq_in_captured = int(round(peak_t_s * sr_eff))

    # IQ peak slice (±25ms 围绕 peak)
    half_n = max(1, int(0.025 * sr_eff))
    lo = max(0, peak_iq_in_captured - half_n)
    hi = peak_iq_in_captured + half_n
    captured_total = pre_iq.size + post_iq.size
    hi = min(captured_total, hi)
    if lo >= captured_total:
        peak_iq = np.zeros(0, dtype=np.complex64)
    elif lo < pre_iq.size:
        pre_part_n = pre_iq.size - lo
        pre_part = pre_iq[-pre_part_n:] if pre_part_n > 0 else np.zeros(0, dtype=np.complex64)
        post_part_n = max(0, hi - pre_iq.size)
        post_part = post_iq[:post_part_n] if post_part_n > 0 else np.zeros(0, dtype=np.complex64)
        peak_iq = np.concatenate([pre_part, post_part]) if pre_part.size + post_part.size > 0 \
                  else np.zeros(0, dtype=np.complex64)
    else:
        s = lo - pre_iq.size
        e = hi - pre_iq.size
        peak_iq = post_iq[s:e]

    # 平均频谱 (用 fft_size 窗口扫一遍 captured IQ, 求平均 |FFT|^2 后转 dB)
    captured = np.concatenate([pre_iq, post_iq]) if post_iq.size > 0 else pre_iq
    fft_size = state.fft_size
    if captured.size < fft_size:
        captured = np.concatenate([np.zeros(fft_size - captured.size, dtype=np.complex64), captured])
    hann = np.hanning(fft_size)
    n_frames = captured.size // fft_size
    if n_frames > 0:
        spec_accum = np.zeros(fft_size, dtype=np.float64)
        for k in range(n_frames):
            seg = captured[k * fft_size:(k + 1) * fft_size] * hann
            spec = np.fft.fftshift(np.fft.fft(seg))
            spec_accum += np.abs(spec) ** 2
        spec_accum /= n_frames
        spec_db = (20 * np.log10(np.sqrt(spec_accum) + 1e-18)).astype(np.float32)
        spec_db -= spec_db.max()
        spec_ds = decimate_maxpool(spec_db, DISPLAY_BINS)
    else:
        spec_ds = np.zeros(DISPLAY_BINS, dtype=np.float32)

    # envelope peak/rms
    if envelope_trace.size > 0:
        peak_env = float(np.max(envelope_trace))
        rms_env = float(np.sqrt(np.mean(envelope_trace ** 2)))
    else:
        peak_env = 0.0
        rms_env = 0.0

    out = {
        "id": ev["id"],
        "t": ev["t"],
        "excess_db": ev["excess_db"],
        "peak_env": round(peak_env, 4),
        "rms_env": round(rms_env, 4),
        "envelope_trace": envelope_trace.tolist(),
        "envelope_rate_hz": env_rate,
        "duration_s": round(envelope_trace.size / env_rate, 3) if env_rate > 0 else 0,
        "freq_center_hz": ev["freq_center_hz"],
        "sample_rate_hz": sample_rate,
        "spectrum": spec_ds.tolist(),
        "peak_idx": peak_idx,
        "peak_t_ms": round(peak_t_s * 1000, 1),
    }
    _flashes.append({
        "id": out["id"],
        "t": out["t"],
        "excess_db": out["excess_db"],
        "peak_env": out["peak_env"],
        "rms_env": out["rms_env"],
    })
    socketio.emit("flash", out)

    _flash_details[out["id"]] = {
        "iq_peak_slice": peak_iq.tolist(),
        "iq_sample_rate_hz": sr_eff,
        "peak_idx": peak_idx,
        "peak_t_ms": out["peak_t_ms"],
    }
    if len(_flash_details) > _MAX_FLASH_CACHE:
        oldest = min(_flash_details.keys())
        _flash_details.pop(oldest, None)


def reader_loop():
    global _total_samples, _flash_count, _t_start
    global _eff_rate, _eff_rate_acc_samples, _eff_rate_last_t
    global _display_env_acc, _display_env_acc_n, _display_env_last_t

    fft_buf = np.zeros(state.fft_size, dtype=np.complex64)
    fft_n = 0
    win = np.hanning(state.fft_size)

    t_last_frame = 0.0
    peak_ema = 0.0
    last_trigger_t = -1e9

    win_acc_power = 0.0
    win_acc_n = 0
    # 窗目标用 _current_eff_rate() (在循环里更新), 首次先按声明速率占位
    win_target_n = max(1, int(state.sample_rate * state.flash_window_ms / 1000))

    post_capture = None
    _eff_rate = 0.0
    _eff_rate_acc_samples = 0
    _eff_rate_last_t = 0.0
    _display_env_acc = 0.0
    _display_env_acc_n = 0
    _display_env_last_t = 0.0

    _rebuild_buffers_for_state()

    _t_start = time.time()
    print(f"[+] reader 启动, fc={state.center_freq/1e3:.1f} kHz, "
          f"sr={state.sample_rate/1e6:.2f} MSPS")

    while not _stop_event.is_set():
        with _sdr_lock, _stream_lock:
            sdr = _sdr
            stream = _stream
        if sdr is None or stream is None:
            time.sleep(0.2)
            continue

        buf = np.empty(state.chunk, dtype=np.complex64)
        try:
            ret = sdr.readStream(stream, [buf], state.chunk, timeoutUs=500000)
        except Exception as e:
            print(f"[!] readStream 异常: {e}")
            time.sleep(0.5)
            continue
        if ret.ret <= 0:
            continue
        seg = buf[: ret.ret]
        _total_samples += ret.ret
        push_iq_ring(seg)

        # 峰值 (给溢出检测用)
        peak_now = float(np.max(np.abs(seg)))
        peak_ema = peak_ema * 0.97 + peak_now * 0.03

        # === 有效采样率估计 (每 1s 一次 EMA) ===
        _eff_rate_acc_samples += ret.ret
        t_now = time.time()
        if _eff_rate_last_t == 0.0:
            _eff_rate_last_t = t_now
        elif t_now - _eff_rate_last_t >= 1.0:
            inst = _eff_rate_acc_samples / (t_now - _eff_rate_last_t)
            if _eff_rate == 0.0:
                _eff_rate = inst
            else:
                _eff_rate = _eff_rate * 0.7 + inst * 0.3
            _eff_rate_acc_samples = 0
            _eff_rate_last_t = t_now
            # 更新窗目标
            win_target_n = max(1, int(_eff_rate * state.flash_window_ms / 1000))

        # === 实时显示用包络 (固定 100 Hz, 墙钟触发, 跟 SDR 采样率解耦) ===
        env_now = float(np.mean(np.abs(seg)))
        _display_env_acc += env_now
        _display_env_acc_n += 1
        if _display_env_last_t == 0.0:
            _display_env_last_t = t_now
        else:
            disp_period = 1.0 / max(1, state.env_rate_hz)
            if t_now - _display_env_last_t >= disp_period:
                if _display_env_acc_n > 0:
                    _display_env_ring.append(_display_env_acc / _display_env_acc_n)
                _display_env_acc = 0.0
                _display_env_acc_n = 0
                _display_env_last_t = t_now

        # 包络降采样进 flash 的 env_ring (max-pool)
        env_rate = state.flash_env_rate_hz
        sr_eff = _current_eff_rate()
        spr = max(1.0, sr_eff / env_rate)
        n_seg = len(seg)
        env_samples = _downsample_envelope_chunk(seg, spr)
        if env_samples:
            _env_ring.extend(env_samples)

        # === 窗累积 + 触发检测 ===
        win_acc_power += float(np.sum(np.abs(seg) ** 2))
        win_acc_n += n_seg
        if win_acc_n >= win_target_n:
            i_k = win_acc_power / win_acc_n
            i_k_db = 10 * np.log10(i_k + 1e-18)
            _intensity_ring.append(i_k_db)
            baseline_db = float(np.mean(_intensity_ring)) if _intensity_ring else -100.0
            excess_db = i_k_db - baseline_db

            in_cd = (t_now - last_trigger_t) * 1000 < state.flash_cooldown_ms
            if (not in_cd) and excess_db > state.flash_thresh_db:
                last_trigger_t = t_now
                _flash_count += 1
                pre_n = int(env_rate * state.flash_pre_ms / 1000)
                pre_env = list(_env_ring)[-pre_n:] if pre_n > 0 else []
                ev = {
                    "id": _flash_count,
                    "t": round(t_now - _t_start, 3),
                    "excess_db": round(excess_db, 1),
                    "pre_env": pre_env,
                    "freq_center_hz": state.center_freq,
                    "sample_rate_hz": state.sample_rate,
                    "window_ms": state.flash_window_ms,
                    "pre_ms": state.flash_pre_ms,
                    "post_ms": state.flash_post_ms,
                    "env_rate_hz": env_rate,
                }
                post_capture = {
                    "ev": ev,
                    "remaining_samples": int(sr_eff * state.flash_post_ms / 1000),
                    "post_env": [],
                    "post_iq_chunks": [],
                }
            win_acc_power = 0.0
            win_acc_n = 0

        # === post-capture ===
        if post_capture is not None:
            if env_samples:
                post_capture["post_env"].extend(env_samples)
            post_capture["post_iq_chunks"].append(seg.copy())
            take = min(post_capture["remaining_samples"], n_seg)
            post_capture["remaining_samples"] -= take
            if post_capture["remaining_samples"] <= 0:
                _finalize_flash(post_capture)
                post_capture = None

        # 累积 FFT (waterfall)
        n = n_seg
        while n > 0:
            space = state.fft_size - fft_n
            take = min(space, n)
            fft_buf[fft_n : fft_n + take] = seg[:take]
            fft_n += take
            seg = seg[take:]
            n -= take
            if fft_n == state.fft_size:
                spec = np.fft.fftshift(np.fft.fft(fft_buf * win))
                psd_db = (20 * np.log10(np.abs(spec) + 1e-12)).astype(np.float32)
                psd_db -= psd_db.max()
                psd_ds = decimate_maxpool(psd_db, DISPLAY_BINS)
                _waterfall.append(psd_ds)
                fft_n = 0

        # 推送到浏览器 (15 fps)
        if t_now - t_last_frame >= FRAME_INTERVAL_S and _waterfall:
            t_last_frame = t_now
            disp_n = int(state.env_window_s * state.env_rate_hz)
            env_disp = list(_display_env_ring)[-disp_n:]
            env_db_mean = 20 * np.log10(np.mean(env_disp) + 1e-12) if env_disp else -100.0
            spec_line = _waterfall[-1]
            overflow = peak_ema > OVERFLOW_THRESHOLD
            socketio.emit(
                "frame",
                {
                    "t": round(t_now - _t_start, 2),
                    "samples": _total_samples,
                    "spectrum": spec_line.tolist(),
                    "envelope": env_disp,
                    "envelope_rate_hz": state.env_rate_hz,
                    "envelope_db_mean": env_db_mean,
                    "flash_count": _flash_count,
                    "peak_ema": round(peak_ema, 3),
                    "overflow": overflow,
                    "running": _running["ok"],
                    "msg": _running["msg"],
                },
            )

    print("[+] reader 退出")


# ============== HTTP / WebSocket ==============
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(state.to_dict())


@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.get_json(force=True) or {}

    restart_needed = False
    with state.lock:
        for k, v in data.items():
            if not hasattr(state, k):
                continue
            if k == "center_freq" and v != state.center_freq:
                state.center_freq = float(v)
            elif k == "bandwidth" and v != state.bandwidth:
                state.bandwidth = float(v)
            elif k in ("ifgr", "rfgr"):
                setattr(state, k, int(v))
            elif k in ("flash_window_ms", "flash_baseline_ms", "flash_pre_ms",
                       "flash_post_ms", "flash_cooldown_ms"):
                setattr(state, k, float(v))
            elif k == "flash_thresh_db":
                state.flash_thresh_db = float(v)
            elif k == "flash_env_rate_hz":
                state.flash_env_rate_hz = max(50.0, float(v))
            elif k == "env_window_s":
                state.env_window_s = max(1.0, min(60.0, float(v)))
                _rebuild_buffers_for_state()
            elif k == "env_rate_hz":
                state.env_rate_hz = max(20.0, min(1000.0, float(v)))
                _rebuild_buffers_for_state()
            elif k == "sample_rate" and float(v) != state.sample_rate:
                state.sample_rate = float(v)
                restart_needed = True
            elif k == "fft_size":
                state.fft_size = int(v)

    if restart_needed:
        reopen_sdr()
    else:
        if apply_config_to_sdr():
            _running["ok"] = True
            _running["msg"] = "ok"

    return jsonify({"ok": _running["ok"], "state": state.to_dict()})


@app.route("/api/waterfall.png")
def api_waterfall_png():
    """导出当前 waterfall 为 PNG（matplotlib）"""
    if not _waterfall:
        return ("no data", 404)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        wf = np.array(_waterfall)  # (T, F)
        fig, ax = plt.subplots(figsize=(10, 6))
        extent = [
            -state.sample_rate / 2 / 1e3,
            state.sample_rate / 2 / 1e3,
            0,
            len(_waterfall) * 0.1,  # 假设每帧 0.1s
        ]
        ax.imshow(
            wf,
            origin="lower",
            aspect="auto",
            extent=extent,
            cmap="viridis",
            vmin=-40,
            vmax=0,
        )
        ax.set_xlabel("Freq (kHz, rel to center)")
        ax.set_ylabel("Time (s, recent first)")
        ax.set_title(
            f"RSP1 waterfall @ {state.center_freq/1e3:.1f} kHz, "
            f"{_flash_count} flashes"
        )
        out = APP_DIR / "static" / "waterfall.png"
        fig.tight_layout()
        fig.savefig(out, dpi=110)
        plt.close(fig)
        return jsonify({"url": "/static/waterfall.png"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@socketio.on("connect")
def on_connect():
    print("[+] 客户端连接")
    # 立即推一份当前 state
    socketio.emit("state", state.to_dict())


@socketio.on("disconnect")
def on_disconnect():
    print("[-] 客户端断开")


@app.route("/api/flash/<int:fid>/detail")
def api_flash_detail(fid):
    """返回指定 flash 的 IQ peak slice (modal lazy-load)"""
    d = _flash_details.get(fid)
    if not d:
        return jsonify({"error": "not found"}), 404
    samples = d["iq_peak_slice"]
    return jsonify({
        "id": fid,
        "peak_idx": d["peak_idx"],
        "peak_t_ms": d["peak_t_ms"],
        "n": len(samples),
        "sample_rate": d["iq_sample_rate_hz"],
        "i": [float(x.real) for x in samples],
        "q": [float(x.imag) for x in samples],
    })


# ============== Main ==============
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-sdr", action="store_true",
                        help="不连硬件，只跑 web 框架（调试用)")
    parser.add_argument("--bench", action="store_true",
                        help="bench 模式: 不开 web, 分阶段计时 reader_loop, "
                             "每秒打印一次统计, 跑 --bench-duration 秒后退出")
    parser.add_argument("--bench-duration", type=float, default=20.0,
                        help="bench 模式运行时长 (秒)")
    parser.add_argument("--bench-flags", default="",
                        help="bench 控制位 (逗号分隔): "
                             "no-fft=跳过 FFT, no-emit=跳过 frame emit, "
                             "no-flash=跳过 flash 检测, no-iq=跳过 IQ ring")
    args = parser.parse_args()

    if args.bench:
        run_bench(args)
        return

    if not args.no_sdr:
        # 尝试打开 SDR；如果失败（设备被占），启动后台重试线程
        try:
            open_sdr()
            _running["ok"] = True
            _running["msg"] = "ok"
        except Exception as e:
            _running["ok"] = False
            _running["msg"] = f"等待设备: {e}"
            print(f"[!] SDR 启动失败: {e}")
            print("[!] 仍会启动 web 框架 + 后台重试连接")
            threading.Thread(target=retry_open_sdr_loop, daemon=True).start()
    else:
        _running["ok"] = False
        _running["msg"] = "no-sdr 模式"

    global _reader_thread
    if not args.no_sdr and _running["ok"]:
        _reader_thread = threading.Thread(target=reader_loop, daemon=True)
        _reader_thread.start()

    print(f"[+] Web 服务: http://{args.host}:{args.port}")
    try:
        socketio.run(
            app,
            host=args.host,
            port=args.port,
            debug=False,
            allow_unsafe_werkzeug=True,  # 单进程 dev 用
        )
    finally:
        _stop_event.set()
        with _stream_lock, _sdr_lock:
            close_sdr_unlocked()
        print("[+] 退出")


# ============== Bench 模式 ==============
def run_bench(args):
    """不开 web, 直接跑 reader_loop, 每秒打印分阶段计时统计."""
    flags = {f.strip() for f in args.bench_flags.split(",") if f.strip()}
    no_fft = "no-fft" in flags
    no_emit = "no-emit" in flags
    no_flash = "no-flash" in flags
    no_iq = "no-iq" in flags
    print(f"[bench] flags: no-fft={no_fft} no-emit={no_emit} "
          f"no-flash={no_flash} no-iq={no_iq}")
    print(f"[bench] fc={state.center_freq/1e3:.1f} kHz, "
          f"sr={state.sample_rate/1e6:.2f} MSPS, "
          f"fft_size={state.fft_size}, chunk={state.chunk}, "
          f"flash_window_ms={state.flash_window_ms}")

    # 打开 SDR
    try:
        open_sdr()
        _running["ok"] = True
        _running["msg"] = "ok"
    except Exception as e:
        print(f"[bench] SDR 打开失败: {e}")
        return

    # 重置全局 bench 计数 + 打开 reader_loop 内部计时
    BENCH = {
        "t_readStream": 0.0,
        "t_env": 0.0,
        "t_flash": 0.0,
        "t_fft": 0.0,
        "t_emit": 0.0,
        "t_iq": 0.0,
        "t_total": 0.0,
        "iters": 0,
        "samples": 0,
        "ffts": 0,
        "emits": 0,
        "reads_zero": 0,
        "t_last": time.perf_counter(),
        "t_start": time.perf_counter(),
    }
    print(f"[bench] running for {args.bench_duration:.0f}s ...")

    # 跑 reader_loop 的镜像 (带计时)
    _bench_loop(no_fft, no_emit, no_flash, no_iq, args.bench_duration)

    # 汇总
    dt_total = time.perf_counter() - BENCH["t_start"]
    iters = BENCH["iters"]
    samples = BENCH["samples"]
    print()
    print("=" * 60)
    print(f"[bench] TOTAL: {dt_total:.2f}s")
    print(f"  iters: {iters} ({iters/dt_total:.0f}/s)")
    print(f"  samples: {samples} ({samples/dt_total:.0f}/s, "
          f"{samples/dt_total/1e6:.3f} MSPS effective)")
    print(f"  FFTs: {BENCH['ffts']} ({BENCH['ffts']/dt_total:.0f}/s)")
    print(f"  emits: {BENCH['emits']} ({BENCH['emits']/dt_total:.2f}/s, "
          f"throttle cap = 15.15/s)")
    print(f"  max_iters_between_emits: {BENCH.get('max_iters_between', 'n/a')}")
    print(f"  max_wall_between_emits (s): {BENCH.get('max_wall_between', 0):.4f}")
    print(f"  reads_zero: {BENCH['reads_zero']}")
    if iters:
        t_total = BENCH["t_total"]
        print(f"  per-iter: {t_total/iters*1000:.2f}ms total")
        for k in ["t_readStream", "t_env", "t_flash", "t_fft", "t_emit", "t_iq"]:
            pct = BENCH[k] / t_total * 100 if t_total else 0
            print(f"    {k:12s} {BENCH[k]/iters*1000:7.2f}ms  ({pct:5.1f}%)")
        residual = (t_total - sum(BENCH[k] for k in
                    ["t_readStream", "t_env", "t_flash", "t_fft", "t_emit", "t_iq"])) \
                    / iters * 1000
        print(f"    {'other':12s} {residual:7.2f}ms")
    print("=" * 60)

    _stop_event.set()
    with _stream_lock, _sdr_lock:
        close_sdr_unlocked()


def _bench_print_stats(BENCH, force=False):
    now = time.perf_counter()
    if not force and now - BENCH["t_last"] < 1.0:
        return
    dt = now - BENCH["t_last"]
    if dt <= 0:
        return
    iters = BENCH["iters"]
    samples = BENCH["samples"]
    print(f"\n[bench +{now - BENCH['t_start']:5.1f}s] "
          f"iters={iters/dt:5.0f}/s samples={samples/dt/1e3:7.1f}kS/s "
          f"FFTs={BENCH['ffts']/dt:4.0f}/s emits={BENCH['emits']/dt:5.2f}/s "
          f"reads0={BENCH['reads_zero']}")
    if iters:
        t_total = BENCH["t_total"]
        print(f"          per-iter {t_total/iters*1000:5.2f}ms | "
              f"read={BENCH['t_readStream']/iters*1000:5.2f} "
              f"env={BENCH['t_env']/iters*1000:4.2f} "
              f"flash={BENCH['t_flash']/iters*1000:4.2f} "
              f"fft={BENCH['t_fft']/iters*1000:5.2f} "
              f"emit={BENCH['t_emit']/iters*1000:4.2f} "
              f"iq={BENCH['t_iq']/iters*1000:4.2f}")
    BENCH["t_readStream"] = 0.0
    BENCH["t_env"] = 0.0
    BENCH["t_flash"] = 0.0
    BENCH["t_fft"] = 0.0
    BENCH["t_emit"] = 0.0
    BENCH["t_iq"] = 0.0
    BENCH["t_total"] = 0.0
    BENCH["iters"] = 0
    BENCH["samples"] = 0
    BENCH["ffts"] = 0
    BENCH["emits"] = 0
    BENCH["reads_zero"] = 0
    BENCH["t_last"] = now


def _bench_loop(no_fft, no_emit, no_flash, no_iq, duration):
    """reader_loop 的镜像, 每阶段 perf_counter 计时."""
    global BENCH
    BENCH = {
        "t_readStream": 0.0, "t_env": 0.0, "t_flash": 0.0,
        "t_fft": 0.0, "t_emit": 0.0, "t_iq": 0.0, "t_total": 0.0,
        "iters": 0, "samples": 0, "ffts": 0, "emits": 0, "reads_zero": 0,
        "emits_would": 0,  # theoretical max at 15 fps
        "t_last": time.perf_counter(),
        "t_start": time.perf_counter(),
        "t_last_frame": 0.0,
    }
    t_end = BENCH["t_start"] + duration
    fft_buf = np.zeros(state.fft_size, dtype=np.complex64)
    fft_n = 0
    win = np.hanning(state.fft_size)
    win_acc_power = 0.0
    win_acc_n = 0
    win_target_n = max(1, int(state.sample_rate * state.flash_window_ms / 1000))

    buf = np.empty(state.chunk, dtype=np.complex64)
    peak_ema = 0.0
    last_trigger_t = -1e9
    post_capture = None

    if not no_iq:
        _rebuild_buffers_for_state()

    while not _stop_event.is_set():
        with _sdr_lock, _stream_lock:
            sdr = _sdr
            stream = _stream
        if sdr is None or stream is None:
            time.sleep(0.2)
            continue

        t_iter = time.perf_counter()
        if t_iter > t_end:
            break

        try:
            ret = sdr.readStream(stream, [buf], state.chunk, timeoutUs=500000)
        except Exception as e:
            print(f"[bench] readStream 异常: {e}")
            time.sleep(0.5)
            continue

        t_after_read = time.perf_counter()
        if ret.ret <= 0:
            BENCH["reads_zero"] += 1
            BENCH["t_readStream"] += t_after_read - t_iter
            _bench_print_stats(BENCH)
            continue
        seg = buf[: ret.ret]
        BENCH["samples"] += ret.ret

        if not no_iq:
            t0 = time.perf_counter()
            push_iq_ring(seg)
            BENCH["t_iq"] += time.perf_counter() - t0

        peak_now = float(np.max(np.abs(seg)))
        peak_ema = peak_ema * 0.97 + peak_now * 0.03
        env_now = float(np.mean(np.abs(seg)))
        _envelope.append(env_now)
        t_after_env = time.perf_counter()
        BENCH["t_env"] += t_after_env - t_after_read

        # flash check (cheap, just comparisons)
        if not no_flash:
            t_now = time.time()
            win_acc_power += float(np.sum(np.abs(seg) ** 2))
            win_acc_n += ret.ret
            if win_acc_n >= win_target_n:
                i_k = win_acc_power / win_acc_n
                i_k_db = 10 * np.log10(i_k + 1e-18)
                _intensity_ring.append(i_k_db)
                baseline_db = float(np.mean(_intensity_ring)) if _intensity_ring else -100.0
                excess_db = i_k_db - baseline_db
                in_cd = (t_now - last_trigger_t) * 1000 < state.flash_cooldown_ms
                if (not in_cd) and excess_db > state.flash_thresh_db:
                    last_trigger_t = t_now
                win_acc_power = 0.0
                win_acc_n = 0
            BENCH["t_flash"] += time.perf_counter() - t_after_env

        # FFT / waterfall
        t_fft_start = time.perf_counter()
        n = ret.ret
        seg_copy = seg
        while n > 0:
            space = state.fft_size - fft_n
            take = min(space, n)
            fft_buf[fft_n:fft_n + take] = seg_copy[:take]
            fft_n += take
            seg_copy = seg_copy[take:]
            n -= take
            if fft_n == state.fft_size:
                if not no_fft:
                    spec = np.fft.fftshift(np.fft.fft(fft_buf * win))
                    psd_db = (20 * np.log10(np.abs(spec) + 1e-12)).astype(np.float32)
                    psd_db -= psd_db.max()
                    psd_ds = decimate_maxpool(psd_db, DISPLAY_BINS)
                    _waterfall.append(psd_ds)
                fft_n = 0
                BENCH["ffts"] += 1
        BENCH["t_fft"] += time.perf_counter() - t_fft_start

        # frame emit (simulate; without a connected socketio client this is a no-op but still time the call)
        t_emit_start = time.perf_counter()
        if not no_emit:
            t_now = time.perf_counter()
            if t_now - BENCH["t_last_frame"] >= 0.066 and _waterfall:
                # 记录两次 emit 之间的 iters 和时间
                iters_between = BENCH["iters"] - BENCH.get("iters_at_last_emit", 0)
                wall_between = t_now - BENCH.get("t_at_last_emit", BENCH["t_start"])
                BENCH.setdefault("max_iters_between", 0)
                BENCH.setdefault("max_wall_between", 0.0)
                if iters_between > BENCH["max_iters_between"]:
                    BENCH["max_iters_between"] = iters_between
                if wall_between > BENCH["max_wall_between"]:
                    BENCH["max_wall_between"] = wall_between
                BENCH["iters_at_last_emit"] = BENCH["iters"]
                BENCH["t_at_last_emit"] = t_now
                BENCH["t_last_frame"] = t_now
                BENCH["emits"] += 1
        BENCH["t_emit"] += time.perf_counter() - t_emit_start

        BENCH["iters"] += 1
        BENCH["t_readStream"] += t_after_read - t_iter
        BENCH["t_total"] += time.perf_counter() - t_iter

        _bench_print_stats(BENCH)


if __name__ == "__main__":
    main()