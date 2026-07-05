#!/usr/bin/env python3
"""
web_monitor.py

单进程 Web 可视化 RSP1 雷电探测。

启动 (任意平台, 假设已激活 conda env `sdr`):
    python scripts/web_monitor.py --port 5000

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
    - sferics 事件日志
    - 控件: 中心频率 / 采样率 / 增益 / sferic 阈值

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
        self.center_freq = 24e3
        self.sample_rate = 10e6   # 默认 10 MSPS（用户偏好）
        self.bandwidth = 0
        self.ifgr = 39
        self.rfgr = 1
        self.sferic_thresh_db = 12.0
        self.sferic_cooldown = 0.3
        self.fft_size = 1024
        self.chunk = 1024
        self.lock = threading.Lock()

    def to_dict(self):
        with self.lock:
            return {
                "center_freq": self.center_freq,
                "sample_rate": self.sample_rate,
                "bandwidth": self.bandwidth,
                "ifgr": self.ifgr,
                "rfgr": self.rfgr,
                "sferic_thresh_db": self.sferic_thresh_db,
                "sferic_cooldown": self.sferic_cooldown,
                "fft_size": self.fft_size,
                "chunk": self.chunk,
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
_envelope = deque(maxlen=600)   # 时域包络
_sferics = deque(maxlen=200)    # 最近检测到的 sferics
_sferic_count = 0
_total_samples = 0
_t_start = 0.0
_latest_spec_db = None          # 最近一帧完整 FFT（用于宽带判据）

# 滚动 IQ 缓冲（最近 ~4096 samples，用于 sferic 详情波形）
_IQ_BUFFER_MAX = 4096
_iq_ring = np.zeros(_IQ_BUFFER_MAX, dtype=np.complex64)
_iq_ring_n = 0
_iq_ring_pos = 0

# sferic 波形缓存（最多保留 50 条，按 ID 索引）
_sferic_waveforms = {}  # id -> {samples, sample_rate}
_MAX_WAVEFORM_CACHE = 50


def push_iq_ring(seg: np.ndarray):
    """把新 samples 写入环形缓冲"""
    global _iq_ring_n, _iq_ring_pos
    n = len(seg)
    if n == 0:
        return
    if n >= _IQ_BUFFER_MAX:
        # 一次性塞不下那么多，只取最后 _IQ_BUFFER_MAX
        seg = seg[-_IQ_BUFFER_MAX:]
        n = _IQ_BUFFER_MAX
    end = _iq_ring_pos + n
    if end <= _IQ_BUFFER_MAX:
        _iq_ring[_iq_ring_pos:end] = seg
    else:
        first = _IQ_BUFFER_MAX - _iq_ring_pos
        _iq_ring[_iq_ring_pos:] = seg[:first]
        _iq_ring[:n - first] = seg[first:]
    _iq_ring_pos = (_iq_ring_pos + n) % _IQ_BUFFER_MAX
    _iq_ring_n = min(_iq_ring_n + n, _IQ_BUFFER_MAX)


def snapshot_iq_ring() -> np.ndarray:
    """返回当前环形缓冲（按时间顺序，最旧到最新）"""
    if _iq_ring_n < _IQ_BUFFER_MAX:
        return _iq_ring[:_iq_ring_n].copy()
    # 已满：从 _iq_ring_pos 到末尾 + 从头到 _iq_ring_pos
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
SFERIC_MIN_WIDE_BINS = 30   # sferic 候选至少要 N 个 bin 超阈值才算宽带（256 bins 的 ~12%）


def decimate_maxpool(spec: np.ndarray, n_out: int) -> np.ndarray:
    """max-pooling 下采样：保留每个块内的峰值"""
    n_in = len(spec)
    if n_in == n_out:
        return spec
    edges = np.linspace(0, n_in, n_out + 1).astype(int)
    edges[-1] = n_in
    return np.maximum.reduceat(spec, edges[:-1])


def reader_loop():
    global _total_samples, _sferic_count, _t_start

    fft_buf = np.zeros(state.fft_size, dtype=np.complex64)
    fft_n = 0
    win = np.hanning(state.fft_size)
    env_window = deque(maxlen=400)
    last_sferic_t = 0.0
    t_last_frame = 0.0
    peak_ema = 0.0          # 指数滑动平均峰值（用于溢出检测）

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

        # --- 包络 ---
        peak_now = float(np.max(np.abs(seg)))
        peak_ema = peak_ema * 0.97 + peak_now * 0.03  # α=0.03, 半衰期 ≈ 1.5s
        env_now = float(np.mean(np.abs(seg)))
        env_window.append(env_now)
        env_mean = float(np.mean(env_window))
        env_db_now = 20 * np.log10(env_now + 1e-12)
        env_db_mean = 20 * np.log10(env_mean + 1e-12)
        excess_db = env_db_now - env_db_mean

        # --- sferics 检测 ---
        t_now = time.time()
        if (excess_db > state.sferic_thresh_db and
                (t_now - last_sferic_t) > state.sferic_cooldown):
            # 宽带判据：候选时刻 FFT 中超均值的 bin 数
            # 真闪电：能量同时在很多 bin 升起（宽带）
            # AM 载波/窄带干扰：只有少数 bin 亮（窄带）
            wide_bins = 0
            if _latest_spec_db is not None:
                # 阈值：相对当前帧最大值 -25 dB（典型闪电能量散布 >25 dB）
                thr_per_bin = _latest_spec_db.max() - 25
                wide_bins = int(np.sum(_latest_spec_db > thr_per_bin))
            is_broadband = wide_bins >= SFERIC_MIN_WIDE_BINS

            if is_broadband:
                last_sferic_t = t_now
                _sferic_count += 1
                peak = float(np.max(np.abs(seg)))
                rms = float(np.sqrt(np.mean(seg.real ** 2 + seg.imag ** 2)))
                ev = {
                    "id": _sferic_count,
                    "t": round(t_now - _t_start, 2),
                    "peak": round(peak, 4),
                    "rms": round(rms, 4),
                    "excess_db": round(excess_db, 1),
                    "wide_bins": wide_bins,
                    "freq_center_hz": state.center_freq,
                    "sample_rate_hz": state.sample_rate,
                    "waveform_url": f"/api/sferic/{_sferic_count}/waveform",
                }
                _sferics.append(ev)
                socketio.emit("sferic", ev)
                wf = snapshot_iq_ring()
                _sferic_waveforms[_sferic_count] = {
                    "samples": wf.tolist(),
                    "sample_rate": state.sample_rate,
                    "n": int(wf.size),
                }
                if len(_sferic_waveforms) > _MAX_WAVEFORM_CACHE:
                    oldest = min(_sferic_waveforms.keys())
                    _sferic_waveforms.pop(oldest, None)

        # 累积 FFT
        n = len(seg)
        while n > 0:
            space = state.fft_size - fft_n
            take = min(space, n)
            fft_buf[fft_n : fft_n + take] = seg[:take]
            fft_n += take
            seg = seg[take:]
            n -= take
            if fft_n == state.fft_size:
                # 计算 FFT
                spec = np.fft.fftshift(np.fft.fft(fft_buf * win))
                psd_db = (20 * np.log10(np.abs(spec) + 1e-12)).astype(np.float32)
                psd_db -= psd_db.max()
                # 下采样到 DISPLAY_BINS（max-pool 保留峰值）
                psd_ds = decimate_maxpool(psd_db, DISPLAY_BINS)
                _waterfall.append(psd_ds)
                # 保存最近一帧 FFT 给 sferic 检测用（宽带判据）
                _latest_spec_db = psd_db
                fft_n = 0

        # --- 推送到浏览器 (15 fps) ---
        if t_now - t_last_frame >= FRAME_INTERVAL_S and _waterfall:
            t_last_frame = t_now
            env_hist = list(env_window)[-80:]
            spec_line = _waterfall[-1]
            overflow = peak_ema > OVERFLOW_THRESHOLD
            socketio.emit(
                "frame",
                {
                    "t": round(t_now - _t_start, 2),
                    "samples": _total_samples,
                    "spectrum": spec_line.tolist(),
                    "envelope": env_hist,
                    "envelope_db_mean": env_db_mean,
                    "sferic_count": _sferic_count,
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
            elif k in ("sferic_thresh_db", "sferic_cooldown"):
                setattr(state, k, float(v))
            elif k == "sample_rate" and float(v) != state.sample_rate:
                state.sample_rate = float(v)
                restart_needed = True  # setSampleRate 通常需要重建流
            elif k == "fft_size":
                state.fft_size = int(v)  # 下次 reader 自然会用新值

    if restart_needed:
        reopen_sdr()  # 失败原因由 reopen_sdr 自己写到 _running
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
            f"{_sferic_count} sferics"
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


@app.route("/api/sferic/<int:sid>/waveform")
def api_sferic_waveform(sid):
    """返回指定 sferic 的 IQ 波形"""
    wf = _sferic_waveforms.get(sid)
    if not wf:
        return jsonify({"error": "not found"}), 404
    # 从事件缓存里拿 t / freq（事件按 append 单调递增，ID 越近越大）
    ev = next((e for e in reversed(_sferics) if e["id"] == sid), None)
    return jsonify({
        "id": sid,
        "t": ev["t"] if ev else 0,
        "n": wf["n"],
        "sample_rate": wf["sample_rate"],
        "i": [float(x.real) for x in wf["samples"]],
        "q": [float(x.imag) for x in wf["samples"]],
    })


# ============== Main ==============
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-sdr", action="store_true",
                        help="不连硬件，只跑 web 框架（调试用）")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()