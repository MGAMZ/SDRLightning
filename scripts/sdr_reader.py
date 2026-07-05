"""SDR reader subprocess target.

跑在独立进程里, link SoapySDR + acquireReadBuffer, 持续把 IQ chunk
推到 multiprocessing.Queue. 主进程 (web_monitor.py) 从 Queue 拉数据.

为什么独立进程:
- SoapySDR 偶发 ~100ms USB 死区 (isochronous transfer) 在 callback 线程里.
  死区不可避免, 但它**不占主进程 GIL** (主进程独立进程有自己 GIL).
- acquireReadBuffer 走 cond.wait_for, 设短 timeoutUs (10ms) 及时检测 stall.
- 进程隔离: sdr_reader 崩溃可独立重启, 不影响主进程 web 服务.

控制协议 (multiprocessing.Pipe, 单向 parent -> child):
- {"type": "configure", "key": "center_freq", "value": Hz}
- {"type": "configure", "key": "sample_rate", "value": Hz}  (重启 stream)
- {"type": "configure", "key": "gain", "value": dB}
- {"type": "shutdown"}
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: F401  (must be first, configures SOAPY_SDR_PLUGIN_PATH)

import SoapySDR
from SoapySDR import SOAPY_SDR_RX


def _log(msg: str) -> None:
    print(f"[sdr_reader pid={os.getpid()}] {msg}", flush=True)


def _open_sdr(center_freq: float, sample_rate: float, gain):
    # enumerate() 返回完整 SoapySDRKwargs (driver, label, serial 等),
    # 直接 dict(driver=...) 缺少必要字段, SoapySDR 找不到 device
    devs = SoapySDR.Device.enumerate({"driver": "sdrplay"})
    if not devs:
        raise RuntimeError("没找到 sdrplay 设备")
    sdr = SoapySDR.Device(devs[0])
    info = sdr.getHardwareInfo()
    _log(f"SDR opened: {info}")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center_freq)
    if gain is not None:
        sdr.setGain(SOAPY_SDR_RX, 0, gain)
    stream = sdr.setupStream(SOAPY_SDR_RX, "CF32")
    sdr.activateStream(stream)
    return sdr, stream


def _configure(sdr, stream, key, value):
    if key == "center_freq":
        sdr.setFrequency(SOAPY_SDR_RX, 0, float(value))
        _log(f"center_freq -> {float(value)/1e6:.3f} MHz")
        return stream
    if key == "sample_rate":
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)
        sdr.setSampleRate(SOAPY_SDR_RX, 0, float(value))
        new_stream = sdr.setupStream(SOAPY_SDR_RX, "CF32")
        sdr.activateStream(new_stream)
        _log(f"sample_rate -> {float(value)/1e6:.3f} MSPS, stream restarted")
        return new_stream
    if key == "gain":
        sdr.setGain(SOAPY_SDR_RX, 0, float(value))
        _log(f"gain -> {float(value)}")
        return stream
    _log(f"unknown config key: {key}")
    return stream


def _drain_pipe(ctrl) -> list:
    msgs = []
    while ctrl.poll():
        try:
            m = ctrl.recv()
        except (EOFError, OSError):
            break
        msgs.append(m)
    return msgs


def reader_main(ctrl, data_q, chunk_size: int = 1024):
    """sdr_reader 主循环. 持续 readStream 推 chunk 到 data_q.

    Args:
        ctrl: parent->child 端, 单向 control Pipe
        data_q: 推 np.ndarray (complex64) 的 multiprocessing.Queue
        chunk_size: 每块 IQ 元素数, 默认 1024

    Note: 我们本来想用 acquireReadBuffer (zero-copy, 跳过 readStream 的 memcpy),
    但当前 SoapySDR 0.8.1 Python 绑定的 void** 转换有 bug (任何 ctypes
    类型都过不去). readStream 内部其实就是 acquireReadBuffer + memcpy,
    功能一样. 100ms USB spike 来自 sdrplay_api callback 线程,
    readStream 和 acquireReadBuffer 都救不了, 真正要改 STREAMING_USB_MODE_BULK=ON
    (重编 SoapySDRPlay3) 才能从结构性上消除.
    """
    # 第一条消息是初始 config
    init = ctrl.recv()
    sdr, stream = _open_sdr(
        init["center_freq"], init["sample_rate"], init.get("gain"),
    )

    mtu = sdr.getStreamMTU(stream)
    _log(f"MTU = {mtu} IQ elements/buffer, slice = {chunk_size}")
    if chunk_size > mtu:
        chunk_size = mtu
        _log(f"chunk_size > mtu, clamp to {mtu}")

    # readStream 用一个 reusable buffer
    buf = np.empty(chunk_size, dtype=np.complex64)
    last_log_t = time.perf_counter()
    total_chunks = 0
    total_stalls = 0
    timeoutUs = 10_000  # 10ms 短 timeout 早检测 stall

    try:
        while True:
            # 1. control 消息 (非阻塞)
            for m in _drain_pipe(ctrl):
                if m.get("type") == "shutdown":
                    _log("shutdown requested")
                    return
                if m.get("type") == "configure":
                    new_stream = _configure(sdr, stream, m["key"], m["value"])
                    if new_stream is not stream:
                        stream = new_stream
                        mtu = sdr.getStreamMTU(stream)
                        if chunk_size > mtu:
                            buf = np.empty(mtu, dtype=np.complex64)
                else:
                    _log(f"unknown control msg: {m}")

            # 2. readStream: 等下一个 chunk
            try:
                ret = sdr.readStream(stream, [buf], chunk_size, timeoutUs=timeoutUs)
            except Exception as e:
                _log(f"readStream 异常: {e}")
                time.sleep(0.1)
                continue

            n = ret.ret
            if n == SoapySDR.SOAPY_SDR_TIMEOUT:
                # 10ms 内没数据, USB stall 或正常间隙
                total_stalls += 1
                continue
            if n < 0:
                _log(f"readStream error: {n}")
                continue

            chunk = buf[:n].copy()
            try:
                data_q.put_nowait(chunk)
            except Exception:
                # queue 满 (主进程慢). 丢这块保实时
                pass
            total_chunks += 1

            now = time.perf_counter()
            if now - last_log_t >= 1.0:
                qsz = "?"
                try:
                    qsz = data_q.qsize()
                except Exception:
                    pass
                _log(f"  chunks={total_chunks}, stalls={total_stalls}, qsize={qsz}")
                total_stalls = 0
                last_log_t = now

    finally:
        try:
            sdr.deactivateStream(stream)
            sdr.closeStream(stream)
        except Exception:
            pass
        _log(f"reader exit. total chunks pushed: {total_chunks}")


# 给 multiprocessing.Process 当 target 用
def _process_target(ctrl, data_q, chunk_size):
    try:
        reader_main(ctrl, data_q, chunk_size)
    except KeyboardInterrupt:
        _log("interrupted")
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
