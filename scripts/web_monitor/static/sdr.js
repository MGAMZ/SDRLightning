// sdr.js — 前端实时显示（性能优化版 v2）
//
// 性能策略：
//   1. WebSocket → 后台接 frame，只更新 pending 缓冲
//   2. requestAnimationFrame → 渲染循环（与 WS 解耦）
//   3. 瀑布用 scroll-and-draw：每帧 2 次 GPU 操作（不再 200 次）
//   4. 频谱用普通 2D context 折线
//   5. 包络只画 80 个点
//   6. 频谱发送前在 server 端降到 256 bins

const socket = io({
    transports: ["websocket"],
    upgrade: false,
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
});

// === 状态 ===
let centerFreq = 50e6;

// === 频率输入：数字框 + Hz/kHz/MHz/GHz 切换按钮 ===
let freqMult = 1e6;
function setActiveFreqUnit(mult) {
    freqMult = mult;
    document.querySelectorAll("#freq-unit-bar button").forEach(btn => {
        btn.classList.toggle("active", parseFloat(btn.dataset.mult) === mult);
    });
}
function setFreqDisplay(hz) {
    let mult;
    if (hz >= 1e9) mult = 1e9;
    else if (hz >= 1e6) mult = 1e6;
    else if (hz >= 1e3) mult = 1e3;
    else mult = 1;
    setActiveFreqUnit(mult);
    $("ctl-freq").value = hz / mult;
}
function readFreqHz() {
    const v = parseFloat($("ctl-freq").value);
    return (isFinite(v) ? v : 0) * freqMult;
}
let sampleRate = 2000000;
let fftSize = 1024;
let displayBins = 256;
let lineHPx = 1;             // 每行像素高（瀑布）
let lastRenderMs = 0;        // 用于统计 fps

// === DOM ===
const $ = (id) => document.getElementById(id);
const wfCanvas = $("waterfall");
const spCanvas = $("spectrum");
const envCanvas = $("env");
const wfCtx = wfCanvas.getContext("2d", { alpha: false });
const spCtx = spCanvas.getContext("2d", { alpha: false });
const envCtx = envCanvas.getContext("2d", { alpha: false });

// 离屏 canvas：单行 spectrum（宽度跟显示一致，每像素插值取色）
const lineCanvas = document.createElement("canvas");
const lineCtx = lineCanvas.getContext("2d", { alpha: false });
let lineImgData = null;
let lineData = null;
function ensureLineCanvas(w) {
    if (lineCanvas.width !== w) {
        lineCanvas.width = w;
        lineCanvas.height = 1;
        lineImgData = lineCtx.createImageData(w, 1);
        lineData = lineImgData.data;
    }
}

function resizeWfStorage(newW, newH) {
    if (wfStorage.width === newW && wfStorage.height === newH) return;
    const oldW = wfStorage.width;
    const oldH = wfStorage.height;
    let tmp = null;
    if (oldW > 0 && oldH > 0 && oldW === newW) {
        tmp = document.createElement("canvas");
        tmp.width = oldW;
        tmp.height = oldH;
        tmp.getContext("2d").drawImage(wfStorage, 0, 0);
    }
    wfStorage.width = newW;
    wfStorage.height = newH;
    wfStorageCtx.fillStyle = "#08090c";
    wfStorageCtx.fillRect(0, 0, newW, newH);
    if (tmp) {
        wfStorageCtx.imageSmoothingEnabled = false;
        if (newH >= oldH) {
            wfStorageCtx.drawImage(tmp, 0, newH - oldH);
        } else {
            wfStorageCtx.drawImage(tmp, 0, oldH - newH, oldW, newH, 0, 0, newW, newH);
        }
    }
}

// 离屏 canvas：瀑布显示尺寸的"数据层"（不做 DPR 缩放，保持低像素数）
const wfStorage = document.createElement("canvas");
const wfStorageCtx = wfStorage.getContext("2d", { alpha: false, willReadFrequently: true });

// colormap (彩虹 jet: 暗→蓝→青→绿→黄→红→白)
const COLORMAP = new Uint8Array(256 * 3);
(function initColormap() {
    // t in [0, 1]
    function jet(t) {
        if (t < 0.125) return [0, 0, Math.floor(0.5 + t * 4 * 200)];
        if (t < 0.375) return [0, Math.floor((t - 0.125) * 4 * 255), 255];
        if (t < 0.625) return [Math.floor((t - 0.375) * 4 * 255), 255, Math.floor((0.625 - t) * 4 * 255)];
        if (t < 0.875) return [255, Math.floor((0.875 - t) * 4 * 255), 0];
        return [Math.floor((1.125 - t) * 4 * 255), 0, 0];
    }
    for (let i = 0; i < 256; i++) {
        const [r, g, b] = jet(i / 255);
        COLORMAP[i*3]   = r;
        COLORMAP[i*3+1] = g;
        COLORMAP[i*3+2] = b;
    }
})();

// === Canvas HiDPI 处理 ===
function fitCanvas(c) {
    const dpr = window.devicePixelRatio || 1;
    const rect = c.getBoundingClientRect();
    // 如果容器还没尺寸，跳过（避免 0×0 卡死）
    if (rect.width < 1 || rect.height < 1) return;
    const newW = Math.floor(rect.width * dpr);
    const newH = Math.floor(rect.height * dpr);
    // 只有尺寸真变化时才重设 c.width/c.height（设值会清空 canvas！）
    if (c.width !== newW || c.height !== newH) {
        c.width = newW;
        c.height = newH;
    }
}
function resizeAll() {
    // fitCanvas 内部已只在尺寸变化时重设 c.width/c.height（避免清空内容）
    fitCanvas(wfCanvas);
    fitCanvas(spCanvas);
    fitCanvas(envCanvas);
    // 瀑布存储层：尺寸真变化时重建，并把旧内容尽量贴回去（保持最新数据贴底）
    const wfRect = wfCanvas.getBoundingClientRect();
    if (wfRect.width >= 1 && wfRect.height >= 1) {
        resizeWfStorage(Math.floor(wfRect.width), Math.floor(wfRect.height));
    }
    // 频谱/包络的清空由 render 函数本身负责（renderSpectrum/renderEnvelope 每帧重画）
    // 这里只确保 lineHPx 初始化
    lineHPx = 1;
}
window.addEventListener("resize", resizeAll);
resizeAll();
// 应用后重测尺寸 + 每 2 秒保险一次（防 Apply 后布局抖动没触发 window.resize）
setInterval(() => { resizeAll(); }, 2000);

// === 数据缓冲 ===
let pendingFrame = null;
let pendingFlashes = [];
let envHistory = [];
let envRate = 100;
let envMeanDb = -100;
let flashCount = 0;
let lastSpec = null;
let rafCount = 0;
let dataFrameCount = 0;
let lastFpsT = performance.now();

// === 瀑布滚动模式（离屏存储 + GPU blit，永不自拷贝） ===
function pushWaterfallLine(spec) {
    const sw = wfStorage.width;
    const sh = wfStorage.height;
    if (sw === 0 || sh === 0) return;
    const lh = Math.max(1, Math.floor(lineHPx));

    // 1) 离屏存储层向上滚 lh 像素（自拷贝在离屏上，没问题——不是主显示 canvas）
    if (lh > 0 && sh > lh) {
        wfStorageCtx.drawImage(
            wfStorage,
            0, lh, sw, sh - lh,
            0, 0, sw, sh - lh
        );
    }

    // 2) 把新一行画到离屏底部：每显示列都按 spectrum 插值取色，colormap 再插值
    ensureLineCanvas(sw);
    for (let col = 0; col < sw; col++) {
        const binF = (col + 0.5) * displayBins / sw;
        const i0 = binF | 0;
        const i1 = i0 < displayBins - 1 ? i0 + 1 : i0;
        const frac = binF - i0;
        const db = spec[i0] * (1 - frac) + spec[i1] * frac;
        let idx = (db + 60) * 255 / 60;
        if (idx < 0) idx = 0; else if (idx > 255) idx = 255;
        const c0 = idx | 0;
        const c1 = c0 < 255 ? c0 + 1 : c0;
        const cfrac = idx - c0;
        const px = col * 4;
        lineData[px]   = COLORMAP[c0*3]   * (1 - cfrac) + COLORMAP[c1*3]   * cfrac;
        lineData[px+1] = COLORMAP[c0*3+1] * (1 - cfrac) + COLORMAP[c1*3+1] * cfrac;
        lineData[px+2] = COLORMAP[c0*3+2] * (1 - cfrac) + COLORMAP[c1*3+2] * cfrac;
        lineData[px+3] = 255;
    }
    lineCtx.putImageData(lineImgData, 0, 0);
    // GPU 1:1 横向贴到离屏底部（只在纵向拉伸lh倍，因 imageSmoothingEnabled=false）
    wfStorageCtx.imageSmoothingEnabled = false;
    wfStorageCtx.drawImage(lineCanvas, 0, 0, sw, 1, 0, sh - lh, sw, lh);

    // 3) 一次性 blit 离屏到主显示 canvas
    wfCtx.imageSmoothingEnabled = false;
    wfCtx.drawImage(wfStorage, 0, 0, sw, sh, 0, 0, wfCanvas.width, wfCanvas.height);

    $("waterfall-info").textContent = `${sh}px × ${sw}px (${displayBins} bins)`;
}

function renderWaterfallClear() {
    wfStorageCtx.fillStyle = "#08090c";
    wfStorageCtx.fillRect(0, 0, wfStorage.width, wfStorage.height);
}

// === 频谱渲染 ===
function renderSpectrum(spec) {
    const w = spCanvas.width;
    const h = spCanvas.height;

    spCtx.fillStyle = "#08090c";
    spCtx.fillRect(0, 0, w, h);

    // 网格
    spCtx.strokeStyle = "#1f2330";
    spCtx.lineWidth = 1;
    for (let db = -60; db <= 0; db += 10) {
        const y = h * (1 - (db + 60) / 60);
        spCtx.beginPath();
        spCtx.moveTo(0, y);
        spCtx.lineTo(w, y);
        spCtx.stroke();
    }

    if (!spec || !spec.length) return;

    spCtx.strokeStyle = "#00dd77";
    spCtx.lineWidth = 1.5;
    spCtx.beginPath();
    const N = spec.length;
    let peakDb = -200;
    for (let i = 0; i < N; i++) {
        const x = (i + 0.5) / N * w;
        const db = Math.max(-60, Math.min(0, spec[i]));
        if (db > peakDb) peakDb = db;
        const y = h * (1 - (db + 60) / 60);
        if (i === 0) spCtx.moveTo(x, y);
        else spCtx.lineTo(x, y);
    }
    spCtx.stroke();

    $("spectrum-info").textContent = `peak=${peakDb.toFixed(1)} dB`;
}

// === 包络渲染 ===
function renderEnvelope() {
    const w = envCanvas.width;
    const h = envCanvas.height;

    envCtx.fillStyle = "#08090c";
    envCtx.fillRect(0, 0, w, h);

    if (envHistory.length < 2) return;

    let eMin = envHistory[0], eMax = envHistory[0];
    for (let i = 1; i < envHistory.length; i++) {
        const v = envHistory[i];
        if (v < eMin) eMin = v;
        if (v > eMax) eMax = v;
    }
    const pad = (eMax - eMin) * 0.1 || 0.001;
    const lo = Math.max(0, eMin - pad);
    const hi = eMax + pad;

    envCtx.strokeStyle = "#dd3355";
    envCtx.lineWidth = 1.2;
    envCtx.beginPath();
    const N = envHistory.length;
    for (let i = 0; i < N; i++) {
        const x = (i + 0.5) / N * w;
        const y = h - (envHistory[i] - lo) / (hi - lo) * h;
        if (i === 0) envCtx.moveTo(x, y);
        else envCtx.lineTo(x, y);
    }
    envCtx.stroke();

    envCtx.strokeStyle = "#aaa";
    envCtx.lineWidth = 1;
    envCtx.setLineDash([4, 4]);
    const meanVal = Math.pow(10, envMeanDb / 20);
    if (meanVal >= lo && meanVal <= hi) {
        const y = h - (meanVal - lo) / (hi - lo) * h;
        envCtx.beginPath();
        envCtx.moveTo(0, y);
        envCtx.lineTo(w, y);
        envCtx.stroke();
    }
    envCtx.setLineDash([]);

    // 时间刻度 (横轴是 env_window_s 秒历史)
    const dur_s = N / envRate;
    envCtx.fillStyle = "#666";
    envCtx.font = "10px monospace";
    envCtx.fillText(`-${dur_s.toFixed(1)} s`, 4, h - 4);
    envCtx.fillText("now", w - 28, h - 4);
    envCtx.fillText(`mean=${envMeanDb.toFixed(1)} dB`, w / 2 - 30, 12);
}

// === Flashes ===
function pushFlash(ev) {
    const tbody = $("flash-table").querySelector("tbody");
    const tr = document.createElement("tr");
    tr.classList.add("flash-new");
    tr.innerHTML = `<td>${ev.id}</td><td>${ev.t.toFixed(2)}</td>` +
                   `<td>${ev.peak_env.toFixed(3)}</td><td>${ev.rms_env.toFixed(3)}</td>` +
                   `<td>+${ev.excess_db.toFixed(1)}</td>`;
    tr.dataset.flashId = ev.id;
    tr.addEventListener("click", () => openFlashDetail(ev));
    tbody.insertBefore(tr, tbody.firstChild);
    while (tbody.rows.length > 50) tbody.deleteRow(tbody.rows.length - 1);
}

function flushFlashes() {
    while (pendingFlashes.length > 0) {
        pushFlash(pendingFlashes.shift());
    }
}

// === FPS 统计 ===
function updateFps() {
    rafCount++;
    const now = performance.now();
    if (now - lastFpsT >= 1000) {
        const rafFps = (rafCount * 1000 / (now - lastFpsT)).toFixed(0);
        const frameFps = (dataFrameCount * 1000 / (now - lastFpsT)).toFixed(1);
        $("fps").textContent = `paint ${rafFps}Hz · data ${frameFps}fps`;
        rafCount = 0;
        dataFrameCount = 0;
        lastFpsT = now;
    }
}

// === 主渲染循环 ===
function renderLoop() {
    if (pendingFrame) {
        const f = pendingFrame;
        pendingFrame = null;
        dataFrameCount++;
        // 瀑布（离屏存储 + 一次 blit）
        pushWaterfallLine(f.spectrum);
        // 频谱
        renderSpectrum(f.spectrum);
        // 包络
        envHistory = f.envelope;
        if (f.envelope_rate_hz) envRate = f.envelope_rate_hz;
        envMeanDb = f.envelope_db_mean;
        renderEnvelope();
        // 状态
        $("samples-total").textContent = `Samples: ${f.samples.toLocaleString()}`;
        flashCount = f.flash_count;
        $("flash-count").textContent = `Flashes: ${flashCount}`;
        if (f.peak_ema !== undefined) {
            const peakPct = (f.peak_ema * 100).toFixed(0);
            const colorClass = f.overflow ? "peak-overload"
                              : (f.peak_ema > 0.7 ? "peak-ok" : "peak-low");
            $("peak-info").innerHTML = `<span class="${colorClass}">Peak: ${peakPct}%</span>`;
        }
        const up = Math.floor(f.t);
        $("uptime").textContent = `Up: ${Math.floor(up/60)}m ${up%60}s`;
        if (!f.running) {
            $("status-text").textContent = f.msg;
            $("status-dot").className = "dot err";
        }
    }
    flushFlashes();
    updateFps();
    requestAnimationFrame(renderLoop);
}
requestAnimationFrame(renderLoop);

// === Flash 详情 modal ===
const flModal = $("flash-modal");
const flEnvCanvas = $("fl-env");
const flSpecCanvas = $("fl-spec");
const flIqCanvas = $("fl-iq");
const flEnvCtx = flEnvCanvas.getContext("2d");
const flSpecCtx = flSpecCanvas.getContext("2d");
const flIqCtx = flIqCanvas.getContext("2d");

function fitCanvas2(c) {
    const dpr = window.devicePixelRatio || 1;
    const rect = c.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return { W: 0, H: 0 };
    const newW = Math.floor(rect.width * dpr);
    const newH = Math.floor(rect.height * dpr);
    if (c.width !== newW || c.height !== newH) {
        c.width = newW;
        c.height = newH;
    }
    c.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    return { W: rect.width, H: rect.height };
}

function openFlashDetail(ev) {
    $("fl-id").textContent = ev.id;
    $("fl-t").textContent = ev.t.toFixed(2);
    const env_rate = ev.envelope_rate_hz || 1000;
    const env_n = (ev.envelope_trace || []).length;
    const env_dur_s = env_n / env_rate;
    $("fl-meta").textContent =
        `peak_env=${ev.peak_env.toFixed(3)}, rms_env=${ev.rms_env.toFixed(3)}, ` +
        `excess=${ev.excess_db >= 0 ? "+" : ""}${ev.excess_db.toFixed(1)} dB · ` +
        `duration=${env_dur_s.toFixed(2)} s · ` +
        `fc=${(ev.freq_center_hz/1e6).toFixed(3)} MHz · ` +
        `sr=${(ev.sample_rate_hz/1e6).toFixed(2)} MSPS`;

    flModal.classList.add("open");

    requestAnimationFrame(() => {
        drawFlashEnvelope(ev, env_rate);
        drawFlashSpectrum(ev);
        // IQ peak slice 是大头, 异步拉
        fetch(`/api/flash/${ev.id}/detail`)
            .then(r => r.json())
            .then(d => {
                if (d.error) {
                    drawFlashIqUnavailable();
                    return;
                }
                drawFlashIqSlice(d);
            })
            .catch(() => drawFlashIqUnavailable());
    });
}

function drawFlashEnvelope(ev, env_rate) {
    const { W, H } = fitCanvas2(flEnvCanvas);
    if (W === 0) return;
    flEnvCtx.fillStyle = "#08090c";
    flEnvCtx.fillRect(0, 0, W, H);

    const trace = ev.envelope_trace || [];
    if (trace.length < 2) return;
    let vMin = trace[0], vMax = trace[0];
    for (let i = 1; i < trace.length; i++) {
        if (trace[i] < vMin) vMin = trace[i];
        if (trace[i] > vMax) vMax = trace[i];
    }
    const pad = (vMax - vMin) * 0.1 || 0.001;
    vMin = Math.max(0, vMin - pad);
    vMax = vMax + pad;

    const xScale = W / (trace.length - 1);
    const yScale = H / (vMax - vMin);

    flEnvCtx.strokeStyle = "#ffcc00";
    flEnvCtx.lineWidth = 1.2;
    flEnvCtx.beginPath();
    for (let i = 0; i < trace.length; i++) {
        const x = i * xScale;
        const y = H - (trace[i] - vMin) * yScale;
        if (i === 0) flEnvCtx.moveTo(x, y); else flEnvCtx.lineTo(x, y);
    }
    flEnvCtx.stroke();

    // 峰值虚线
    const peakIdx = ev.peak_idx;
    flEnvCtx.strokeStyle = "#ff8844";
    flEnvCtx.setLineDash([4, 4]);
    flEnvCtx.beginPath();
    flEnvCtx.moveTo(peakIdx * xScale, 0);
    flEnvCtx.lineTo(peakIdx * xScale, H);
    flEnvCtx.stroke();
    flEnvCtx.setLineDash([]);

    // 触发时刻 (T_pre 边界)
    const preMs = ev.pre_ms;
    const triggerIdx = (preMs / 1000) * env_rate;
    flEnvCtx.strokeStyle = "#5577ff";
    flEnvCtx.lineWidth = 1;
    flEnvCtx.setLineDash([2, 4]);
    flEnvCtx.beginPath();
    flEnvCtx.moveTo(triggerIdx * xScale, 0);
    flEnvCtx.lineTo(triggerIdx * xScale, H);
    flEnvCtx.stroke();
    flEnvCtx.setLineDash([]);

    flEnvCtx.fillStyle = "#888";
    flEnvCtx.font = "10px monospace";
    flEnvCtx.fillText("← pre-trigger  |trigger|  post-trigger →", 4, 12);
    flEnvCtx.fillText(`peak=${ev.peak_env.toFixed(3)} @ ${ev.peak_t_ms.toFixed(1)} ms`, 4, H - 4);
    flEnvCtx.fillText(`+${(env_dur_s*1000).toFixed(0)} ms`, W - 50, H - 4);
    const env_dur_s_val = trace.length / env_rate;
    void env_dur_s_val;
}

function drawFlashSpectrum(ev) {
    const { W, H } = fitCanvas2(flSpecCanvas);
    if (W === 0) return;
    flSpecCtx.fillStyle = "#08090c";
    flSpecCtx.fillRect(0, 0, W, H);

    const spec = ev.spectrum || [];
    if (spec.length < 2) return;
    let vMin = spec[0], vMax = spec[0];
    for (let i = 1; i < spec.length; i++) {
        if (spec[i] < vMin) vMin = spec[i];
        if (spec[i] > vMax) vMax = spec[i];
    }
    const xScale = W / (spec.length - 1);
    const yScale = H / (vMax - vMin || 1);

    flSpecCtx.strokeStyle = "#00dd77";
    flSpecCtx.lineWidth = 1.2;
    flSpecCtx.beginPath();
    for (let i = 0; i < spec.length; i++) {
        const x = i * xScale;
        const y = H - (spec[i] - vMin) * yScale;
        if (i === 0) flSpecCtx.moveTo(x, y); else flSpecCtx.lineTo(x, y);
    }
    flSpecCtx.stroke();

    flSpecCtx.fillStyle = "#888";
    flSpecCtx.font = "10px monospace";
    flSpecCtx.fillText(`peak ${vMax.toFixed(1)} dB`, 4, 12);
}

function drawFlashIqSlice(d) {
    const { W, H } = fitCanvas2(flIqCanvas);
    if (W === 0) return;
    flIqCtx.fillStyle = "#08090c";
    flIqCtx.fillRect(0, 0, W, H);

    const n = d.n || 0;
    if (n < 2) return;
    const half = H / 2;
    let peakVal = 0;
    const mags = new Float32Array(n);
    for (let i = 0; i < n; i++) {
        const m = Math.sqrt(d.i[i]*d.i[i] + d.q[i]*d.q[i]);
        mags[i] = m;
        if (m > peakVal) peakVal = m;
    }
    const maxAbs = Math.max(peakVal, 1e-9);
    const xScale = W / (n - 1);
    const yScale = (half * 0.85) / maxAbs;

    // 中线
    flIqCtx.strokeStyle = "#1f2330";
    flIqCtx.lineWidth = 1;
    flIqCtx.beginPath(); flIqCtx.moveTo(0, half); flIqCtx.lineTo(W, half); flIqCtx.stroke();

    // 峰值位置
    const peakIdx = d.peak_idx || 0;
    flIqCtx.strokeStyle = "#ffcc00";
    flIqCtx.setLineDash([4, 4]);
    flIqCtx.beginPath();
    flIqCtx.moveTo(peakIdx * xScale, 0);
    flIqCtx.lineTo(peakIdx * xScale, H);
    flIqCtx.stroke();
    flIqCtx.setLineDash([]);

    // I (上半绿)
    flIqCtx.strokeStyle = "#00dd77";
    flIqCtx.lineWidth = 1;
    flIqCtx.beginPath();
    for (let i = 0; i < n; i++) {
        const x = i * xScale;
        const y = half / 2 - d.i[i] * yScale;
        if (i === 0) flIqCtx.moveTo(x, y); else flIqCtx.lineTo(x, y);
    }
    flIqCtx.stroke();

    // Q (上半红)
    flIqCtx.strokeStyle = "#dd3355";
    flIqCtx.beginPath();
    for (let i = 0; i < n; i++) {
        const x = i * xScale;
        const y = half / 2 - d.q[i] * yScale;
        if (i === 0) flIqCtx.moveTo(x, y); else flIqCtx.lineTo(x, y);
    }
    flIqCtx.stroke();

    // |IQ| (下半黄)
    flIqCtx.strokeStyle = "#ffcc00";
    flIqCtx.lineWidth = 1.4;
    flIqCtx.beginPath();
    for (let i = 0; i < n; i++) {
        const x = i * xScale;
        const y = H - mags[i] * yScale * 0.9 - 6;
        if (i === 0) flIqCtx.moveTo(x, y); else flIqCtx.lineTo(x, y);
    }
    flIqCtx.stroke();

    const dt_ms = 1000 / d.sample_rate;
    flIqCtx.fillStyle = "#888";
    flIqCtx.font = "10px monospace";
    flIqCtx.fillText("I (绿) / Q (红) 时域,  |IQ| (黄)", 4, 12);
    flIqCtx.fillText(`0 ms`, 4, H - 4);
    flIqCtx.fillText(`${(n * dt_ms).toFixed(1)} ms`, W - 50, H - 4);
}

function drawFlashIqUnavailable() {
    const { W, H } = fitCanvas2(flIqCanvas);
    if (W === 0) return;
    flIqCtx.fillStyle = "#08090c";
    flIqCtx.fillRect(0, 0, W, H);
    flIqCtx.fillStyle = "#888";
    flIqCtx.font = "11px monospace";
    flIqCtx.fillText("IQ 切片未缓存 (事件太老或后端已重启)", 10, H / 2);
}

function closeFlashDetail() {
    flModal.classList.remove("open");
}

$("fl-close").addEventListener("click", closeFlashDetail);
flModal.addEventListener("click", (e) => {
    if (e.target === flModal) closeFlashDetail();
});

// === 卡片折叠/展开 ===
const CARD_ROW_SIZE = {
    waterfall: 'minmax(80px, 1fr)',
    spectrum:  'minmax(80px, 1fr)',
    env:       '130px',
    flash:    'minmax(100px, 220px)',
};
const CARD_ORDER = ['waterfall', 'spectrum', 'env', 'flash'];

function updateDisplayLayout() {
    const rows = CARD_ORDER.map(name => {
        const card = document.getElementById(`card-${name}`);
        return card && card.classList.contains('collapsed') ? 'auto' : CARD_ROW_SIZE[name];
    });
    const display = document.querySelector('.display');
    if (display) display.style.gridTemplateRows = rows.join(' ');
}

function toggleCollapse(card) {
    if (!card) return;
    card.classList.toggle("collapsed");
    updateDisplayLayout();
    requestAnimationFrame(resizeAll);
}

document.querySelectorAll(".card-header").forEach(h => {
    h.addEventListener("click", (e) => {
        const card = h.closest(".card");
        if (!card) return;
        if (e.target.closest("a, button, input, select, textarea")) return;
        toggleCollapse(card);
    });
});
updateDisplayLayout();

// === 问号 tooltip（动态浮动，绕过 overflow 裁剪） ===
function initTooltips() {
    document.querySelectorAll(".tip").forEach(tip => {
        let popup = null;
        tip.addEventListener("mouseenter", () => {
            if (popup) return;
            const rect = tip.getBoundingClientRect();
            popup = document.createElement("div");
            popup.className = "tip-popup";
            popup.textContent = tip.dataset.tip;
            // 放在 ? 下方；如果下方空间不够就放上方
            let top = rect.bottom + 6;
            const popupHeight = 80; // 估算
            if (top + popupHeight > window.innerHeight) {
                top = rect.top - popupHeight - 6;
            }
            // 如果右侧会超出 viewport，左移
            let left = rect.left;
            if (left + 240 > window.innerWidth) {
                left = window.innerWidth - 250;
            }
            popup.style.left = left + "px";
            popup.style.top = top + "px";
            document.body.appendChild(popup);
        });
        tip.addEventListener("mouseleave", () => {
            if (popup) { popup.remove(); popup = null; }
        });
    });
}
initTooltips();

// === Socket 事件 ===
socket.on("connect", () => {
    $("status-text").textContent = "已连接";
    $("status-dot").className = "dot ok";
});

socket.on("disconnect", () => {
    $("status-text").textContent = "断开";
    $("status-dot").className = "dot err";
});

socket.on("state", (s) => {
    centerFreq = s.center_freq;
    sampleRate = s.sample_rate;
    fftSize = s.fft_size;
    setFreqDisplay(s.center_freq);
    $("ctl-sr").value = s.sample_rate;
    $("ctl-ifgr").value = s.ifgr;
    $("val-ifgr").textContent = s.ifgr;
    $("ctl-rfgr").value = s.rfgr;
    $("val-rfgr").textContent = s.rfgr;
    $("ctl-env-window-s").value = s.env_window_s;
    $("ctl-window-ms").value = s.flash_window_ms;
    $("ctl-baseline-ms").value = s.flash_baseline_ms;
    $("ctl-thresh").value = s.flash_thresh_db;
    $("ctl-pre-ms").value = s.flash_pre_ms;
    $("ctl-post-ms").value = s.flash_post_ms;
    $("ctl-cooldown-ms").value = s.flash_cooldown_ms;
    $("sample-rate").textContent = `SR: ${(s.sample_rate/1e6).toFixed(2)} MSPS`;
});

socket.on("frame", (f) => {
    pendingFrame = f;
});

socket.on("flash", (ev) => {
    pendingFlashes.push(ev);
});

// === UI 控制 ===
function applyControls() {
    const msg = $("ctl-msg");
    const freqHz = readFreqHz();
    if (!isFinite(freqHz) || freqHz <= 0) {
        msg.textContent = "✗ 频率必须为正数";
        msg.className = "hint err";
        return;
    }
    const payload = {
        center_freq: freqHz,
        sample_rate: parseFloat($("ctl-sr").value),
        ifgr: parseInt($("ctl-ifgr").value),
        rfgr: parseInt($("ctl-rfgr").value),
        env_window_s: parseFloat($("ctl-env-window-s").value),
        flash_window_ms: parseFloat($("ctl-window-ms").value),
        flash_baseline_ms: parseFloat($("ctl-baseline-ms").value),
        flash_thresh_db: parseFloat($("ctl-thresh").value),
        flash_pre_ms: parseFloat($("ctl-pre-ms").value),
        flash_post_ms: parseFloat($("ctl-post-ms").value),
        flash_cooldown_ms: parseFloat($("ctl-cooldown-ms").value),
    };
    msg.textContent = "应用…";
    msg.className = "hint";
    fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(j => {
        if (j.ok) {
            centerFreq = payload.center_freq;
            sampleRate = payload.sample_rate;
            msg.textContent = `✓ 已应用 → ${payload.center_freq.toLocaleString()} Hz`;
            msg.className = "hint";
            $("sample-rate").textContent = `SR: ${(payload.sample_rate/1e6).toFixed(2)} MSPS`;
            // 状态变化可能导致布局抖动，强制重测尺寸
            requestAnimationFrame(() => {
                resizeAll();
                renderWaterfallClear();
            });
        } else {
            msg.textContent = "✗ " + (j.error || "失败");
            msg.className = "hint err";
        }
    }).catch(e => {
        msg.textContent = "✗ " + e;
        msg.className = "hint err";
    });
}

document.querySelectorAll("#freq-unit-bar button").forEach(btn => {
    btn.addEventListener("click", () => {
        const hz = readFreqHz();
        setActiveFreqUnit(parseFloat(btn.dataset.mult));
        $("ctl-freq").value = hz / freqMult;
    });
});

$("apply-btn").addEventListener("click", applyControls);
$("ctl-ifgr").addEventListener("input", e => { $("val-ifgr").textContent = e.target.value; });
$("ctl-rfgr").addEventListener("input", e => { $("val-rfgr").textContent = e.target.value; });

document.querySelectorAll(".preset").forEach(b => {
    b.addEventListener("click", () => {
        // 只改中心频率，不动采样率（用户自行决定 SR）
        setFreqDisplay(parseFloat(b.dataset.freq));
        applyControls();
    });
});

$("export-btn").addEventListener("click", () => {
    fetch("/api/waterfall.png").then(r => {
        if (!r.ok) throw new Error("no data");
        return r.json();
    }).then(j => {
        const a = document.createElement("a");
        a.href = j.url + "?t=" + Date.now();
        a.download = "waterfall.png";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }).catch(e => { alert("导出失败: " + e); });
});

// 初始 state
fetch("/api/state").then(r => r.json()).then(s => {
    centerFreq = s.center_freq;
    sampleRate = s.sample_rate;
    fftSize = s.fft_size;
    setFreqDisplay(s.center_freq);
    $("ctl-sr").value = s.sample_rate;
    $("ctl-ifgr").value = s.ifgr;
    $("val-ifgr").textContent = s.ifgr;
    $("ctl-rfgr").value = s.rfgr;
    $("val-rfgr").textContent = s.rfgr;
    $("ctl-env-window-s").value = s.env_window_s;
    $("ctl-window-ms").value = s.flash_window_ms;
    $("ctl-baseline-ms").value = s.flash_baseline_ms;
    $("ctl-thresh").value = s.flash_thresh_db;
    $("ctl-pre-ms").value = s.flash_pre_ms;
    $("ctl-post-ms").value = s.flash_post_ms;
    $("ctl-cooldown-ms").value = s.flash_cooldown_ms;
    $("sample-rate").textContent = `SR: ${(s.sample_rate/1e6).toFixed(2)} MSPS`;
});