# AGENTS.md — SDRplay RSP1 Lightning Detector

## What this repo is

Multi-process Web SDR for an SDRplay RSP1 used as a VLF sferics (lightning) detector. Runs on **Windows (native) or Linux/WSL2**. Architecture: `scripts/sdr_reader.py` 子进程 (readStream → `multiprocessing.Queue`) → `scripts/web_monitor.py` 主进程 (FFT/env/flash/Flask-SocketIO) → browser canvas. Main entrypoint: `scripts/web_monitor.py`.

## Hardware/software chain (load-bearing)

### Linux / WSL2 path

```
RSP1 (USB) ──usbipd-win──> WSL2 (lsusb) ──sdrplay_apiService──> /dev/shm IPC
                                                              └→ libsdrplay_api.so (in /usr/local/lib)
                                                              └→ SoapySDRPlay3 module (third_party/install/lib/SoapySDR/modules0.8/)
                                                              └→ Python (conda env "sdr")
```

### Windows path

```
RSP1 (USB) ──SDRplay API Windows service──> sdrplay_api.dll (C:\Program Files\SDRplay\API\x64\)
                                          └→ SoapySDRPlay3 module (C:\Program Files\PothosSDR\SoapySDR\lib\SoapySDR\modules0.8\)
                                          └→ Python (conda env "sdr")
```

All pieces must work or `SoapySDRUtil --probe="driver=sdrplay"` shows nothing.

## Critical quirks (these took hours to find — do not "fix" without reading this)

1. **Gain order matters**: `setGainMode(SOAPY_Rx, 0, False)` MUST run before any `setGain(...)`. With AGC on, SoapySDRPlay3 silently ignores `setGain` calls.
2. **`STREAMING_USB_MODE_BULK=ON` 必须开** (`third_party/SoapySDRPlay3/CMakeLists.txt:42`，默认 ON)。OFF (默认 ISO) 在 WSL2 USB passthrough 下 0.5% reads 会卡 100ms+，SDR 实际只能 1.5 MSPS / 8 emit/s。ON 后: 10.25 MSPS, 15 emit/s, vslow=0。重建命令: `rm -rf build && mkdir build && cd build && cmake -DSTREAMING_USB_MODE_BULK=ON .. && make -j4 && cp libsdrPlaySupport.so ../../install/lib/SoapySDR/modules0.8/`。
3. **One device, one client**: on Linux, `sdrplay_apiService` serializes USB access. Close `realtime_monitor.py` before opening `web_monitor.py` or vice versa — the second one will silently fail to enumerate. On Windows the SDRplay API service is more forgiving but same single-device rule applies.
4. **Canvas self-copy is a black-canvas trap**: `ctx.drawImage(canvas, ...)` on the same canvas produces blank output in some GPU paths. Waterfall uses an offscreen storage canvas (`wfStorage`) + single blit per frame.
5. **`canvas.width = N` ALWAYS clears**: even if size unchanged. Guard with `if (c.width !== newW || c.height !== newH)` before assigning, or 2-second `setInterval(resizeAll)` will erase the waterfall periodically. (This bug actually shipped — see `scripts/web_monitor/static/sdr.js` `fitCanvas`.)
6. **`fitCanvas` must run after modal is visible**: `getBoundingClientRect()` returns 0 for `display:none` elements. Sferic modal uses `requestAnimationFrame` to defer sizing until after `classList.add('open')`.

## Development environment

### Cross-platform env setup

All Python scripts `import _env` first. This module:
- Detects platform (`sys.platform`)
- Adds `third_party/install/lib/SoapySDR/modules0.8` to `SOAPY_SDR_PLUGIN_PATH` (path sep: `;` on Windows, `:` on Linux) — only if the dir exists
- Adds `third_party/install/lib` to `PATH` (Windows) / `LD_LIBRARY_PATH` (Linux) — only if `libsdrplay_api.*` is there
- On Windows, prepends `<conda env>\Library\bin` to `PATH` so `SoapySDR.dll` is findable
- Idempotent (`_setup_done` flag), safe to import multiple times

This means `python scripts/web_monitor.py` works without any prior env setup beyond `conda activate sdr`.

### Windows

- **Python interpreter**: conda env `sdr` at `D:\Miniforge\envs\sdr` (Python 3.12). Has `SoapySDR`, `numpy`, `scipy`, `matplotlib`, `flask`, `flask-socketio`, `plotext`.
- **Activate**:
  ```powershell
  conda activate sdr
  ```
- **SDRplay service**: installed automatically by the official Windows MSI. Check with `. scripts\env.ps1 -CheckService`. Start with `. scripts\env.ps1 -StartService` (admin).
- **No `sdrplay_apiService` daemon** — Windows version of the API runs as a Windows service.
- **Detail in `docs/WINDOWS.md`.**

### Linux / WSL2

- **Python interpreter**: conda env `sdr` at `/home/mgam/miniforge3/envs/sdr` (Python 3.12). Same packages.
- **Activate + env vars**:
  ```bash
  conda activate sdr
  ```
  `scripts/_env.py` auto-sets `SOAPY_SDR_PLUGIN_PATH`. The legacy `source scripts/env.sh` still works but is no longer required.
- **Daemon** (must be running as root before SDR access):
  ```bash
  sudo nohup bash scripts/start_sdrplay_service.sh > /tmp/sdrplay_service.log 2>&1 &
  disown
  ```
  Bridged USB→shared-memory IPC. Persists across web_monitor restarts. Use `/mnt/c/Program Files/usbipd-win/usbipd.exe` to manage Windows-side USB attach/detach.
- **No tests / no lint / no typecheck** — this is a research script. Verify by running.

## Entry points

| Goal | Command (Windows) | Command (Linux/WSL2) |
|---|---|---|
| Sanity check hardware | `python scripts\test_read_iq.py` | `python3 scripts/test_read_iq.py` |
| Terminal ASCII monitor | `python scripts\realtime_monitor.py` | `python3 scripts/realtime_monitor.py` |
| Web UI (main path) | `python scripts\web_monitor.py --port 5000` | `python3 scripts/web_monitor.py --port 5000` |
| Minimal probe | `python scripts\minimal_probe.py` | `python3 scripts/minimal_probe.py` |
| Raw IQ capture | `python scripts\raw_capture.py -o data\x.cfile` | `python3 scripts/raw_capture.py -o data/x.cfile` |
| Export waterfall PNG | Web UI button, or `curl localhost:5000/api/waterfall.png` | same |

Web UI default `--host 127.0.0.1` (loopback only). For LAN access add `--host 0.0.0.0`.

## Detection algorithm (in `scripts/web_monitor.py` `reader_loop`)

Sferic = `(envelope exceeds moving average by N dB) AND (current FFT has ≥30 bins above max−25 dB)`.

The broadband check is essential — without it, AM broadcast carriers (and their AM modulation envelopes) trigger on every peak. With it, narrowband interference is rejected; only true broadband impulses qualify. Threshold tunable via `/api/control` (key `sferic_thresh_db`).

## IFGR / RFGR sign convention

- **IFGR (20–59 dB)**: gain **reduction**. Higher = more attenuation = weaker signal (lower Peak%). My earlier tooltip had this reversed.
- **RFGR (0–3)**: LNA state. 0 = max gain, 3 = max attenuation.

## File map (only what's load-bearing)

- `scripts/web_monitor.py` — 主进程: Flask + SocketIO + multiprocessing.Process(spawn sdr_reader)。`SDRState` holds hot-reload config; `_iq_ring` (deque of 4096 complex64) feeds sferic waveform cache.
- `scripts/sdr_reader.py` — 子进程 (spawn by web_monitor): `multiprocessing.Queue` 推 IQ chunks 给主进程; `multiprocessing.Pipe` 收主进程 configure 消息。通讯协议见文件顶 docstring.
- `scripts/web_monitor/static/sdr.js` — frontend. `renderLoop` runs at rAF; `pushWaterfallLine` does 2 GPU ops (scroll + new line). `socket.on("frame")` only assigns `pendingFrame`; renderLoop consumes it.
- `scripts/web_monitor/templates/index.html` — single page; `?` icons use **JS-positioned `position:fixed` tooltip** (CSS `::after` was clipped by parent's `overflow-y: auto`).
- `third_party/install/lib/SoapySDR/modules0.8/libsdrPlaySupport.so` — built from `third_party/SoapySDRPlay3/`. Don't replace unless rebuilding.
- `docs/WSL2_USB.md` — usbipd-win 5.x syntax (`bind` then `attach --wsl`, NOT `usbipd wsl attach`).
- `docs/RESEARCH.md` — frequency plan and related work (Blitzortung, etc.).

## 代码开发风格

### 编程守则

- YAGNI：不为未遇到的错误 / 类型不匹配建保护。
- Fast Fail：不吞异常；让 `BaseProcessor.__call__` 统一记录（`processor/base.py:57-61`）。
- 单一调用方倾向内联；重复出现再抽。
- PEP 8 + Python 3.12+ 现代类型注解（pyright 视 3.14，路径要兼容 3.12）。
- 命名：语义清晰性高于精简。
- 代码风格要参考已有内容，整体保持一致。
- 完全不需要保留先后兼容，以代码简洁且容易理解优先。

### 减少不必要信息

- 任何由你最终落地的内容都只写结论语句，不写思考过程或者临时调试备忘。（用户指定等特别情况除外）
- 代码与文档只描述它是什么，不描述它不是什么、不是谁。
- 不写过程性或排除性的注释。
  - 如：删除某项功能后,不要在原地或 docstring 里补一句"本模块不负责 XX,由 XX 阶段 / 兄弟模块负责"。
  - 若所有平级类都需要这种提示，说明这种提示不是信息而是不必要的废话。
- 减少写「未涉及 / 不受影响」的内容。对于完成的一项工作，只总结工作内容本身，不需要列举"没改动的文件 / 没触碰的模块"。
  - 例外:省略会让读者误判改动边界(如"对外 API 不变")、评审者明确要求、安全/兼容等硬性约束,才写未涉及清单。
- 描述做了什么，减少描述没做什么。
- 不要用注释描述代码的逻辑细节，除非这里的设计思路不易于理解。

### 实现前调研

仓库内已有 → Python 生态已有 → 联网查业内做法 → 都不可行才自造

## 基于数据事实

当基于程序的输出结果进行分析时，你不应当进行随意猜测，所有的科学分析都要基于数据事实并结合搜索相关权威资料。
你应当要积极地从当前程序中运行程序并获取实质性数据，提高你的结论的准确性和科学性。
