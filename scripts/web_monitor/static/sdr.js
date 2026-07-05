// sdr.js — 前端实时显示（性能优化版 v2）
//
// 性能策略：
//   1. WebSocket → 后台接 frame，只更新 pending 缓冲
//   2. requestAnimationFrame → 渲染循环（与 WS 解耦）
//   3. 瀑布用 scroll-and-draw：每帧 2 次 GPU 操作（不再 200 次）
//   4. 频谱用普通 2D context 折线
//   5. 包络只画 80 个点
//   6. 频谱发送前在 server 端降到 256 bins

const socket = io();

// === 状态 ===
let centerFreq = 50e6;

// === 频率解析 / 格式化（支持 K / M / G 后缀，单位必须大写） ===
function parseFreq(s) {
    if (s == null) return NaN;
    s = String(s).trim().replace(/\s+/g, "");
    const m = s.match(/^([0-9]*\.?[0-9]+)\s*([kKMG]?)$/);
    if (!m) return NaN;
    const num = parseFloat(m[1]);
    if (!isFinite(num)) return NaN;
    const mult = { "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9 }[m[2]];
    return num * mult;
}
function formatFreq(hz) {
    if (!isFinite(hz) || hz < 0) return String(hz);
    let val, unit;
    if (hz >= 1e9)      { val = hz / 1e9; unit = "G"; }
    else if (hz >= 1e6) { val = hz / 1e6; unit = "M"; }
    else if (hz >= 1e3) { val = hz / 1e3; unit = "k"; }
    else                { val = hz;       unit = ""; }
    return val.toFixed(3).replace(/\.?0+$/, "") + unit;
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
let pendingSferics = [];
let envHistory = [];
let envMeanDb = -100;
let sfericCount = 0;
let lastSpec = null;
let frameCount = 0;
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
    const pad = (eMax - eMin) * 0.1 || 0.01;
    const lo = Math.max(0, eMin - pad);
    const hi = eMax + pad;

    envCtx.strokeStyle = "#dd3355";
    envCtx.lineWidth = 1.2;
    envCtx.beginPath();
    for (let i = 0; i < envHistory.length; i++) {
        const x = (i + 0.5) / envHistory.length * w;
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

    $("env-info").textContent = `mean=${envMeanDb.toFixed(1)} dB`;
}

// === Sferics ===
function pushSferic(ev) {
    const tbody = $("sferic-table").querySelector("tbody");
    const tr = document.createElement("tr");
    tr.classList.add("sferic-new");
    tr.innerHTML = `<td>${ev.id}</td><td>${ev.t.toFixed(2)}</td>` +
                   `<td>${ev.peak.toFixed(3)}</td><td>${ev.rms.toFixed(3)}</td>` +
                   `<td>+${ev.excess_db.toFixed(1)}</td>`;
    tr.dataset.sfericId = ev.id;
    tr.addEventListener("click", () => openSfericDetail(ev.id));
    tbody.insertBefore(tr, tbody.firstChild);
    while (tbody.rows.length > 50) tbody.deleteRow(tbody.rows.length - 1);
}

function flushSferics() {
    while (pendingSferics.length > 0) {
        pushSferic(pendingSferics.shift());
    }
}

// === FPS 统计 ===
function updateFps() {
    frameCount++;
    const now = performance.now();
    if (now - lastFpsT >= 1000) {
        const fps = (frameCount * 1000 / (now - lastFpsT)).toFixed(1);
        $("fps").textContent = `FPS: ${fps}`;
        frameCount = 0;
        lastFpsT = now;
    }
}

// === 主渲染循环 ===
function renderLoop() {
    if (pendingFrame) {
        const f = pendingFrame;
        pendingFrame = null;
        // 瀑布（离屏存储 + 一次 blit）
        pushWaterfallLine(f.spectrum);
        // 频谱
        renderSpectrum(f.spectrum);
        // 包络
        envHistory = f.envelope;
        envMeanDb = f.envelope_db_mean;
        renderEnvelope();
        // 状态
        $("samples-total").textContent = `Samples: ${f.samples.toLocaleString()}`;
        sfericCount = f.sferic_count;
        $("sferic-count").textContent = `Sferics: ${sfericCount}`;
        if (f.peak_ema !== undefined) {
            const peakPct = (f.peak_ema * 100).toFixed(0);
            const colorClass = f.peak_ema > 0.95 ? "peak-warn" : (f.peak_ema > 0.7 ? "peak-ok" : "peak-low");
            $("peak-info").innerHTML = `<span class="${colorClass}">Peak: ${peakPct}%</span>`;
            checkOverflow(f.peak_ema, f);
        }
        const up = Math.floor(f.t);
        $("uptime").textContent = `Up: ${Math.floor(up/60)}m ${up%60}s`;
        if (!f.running) {
            $("status-text").textContent = f.msg;
            $("status-dot").className = "dot err";
        }
    }
    flushSferics();
    updateFps();
    requestAnimationFrame(renderLoop);
}
requestAnimationFrame(renderLoop);

// === Sferic 详情 modal ===
const sfModal = $("sferic-modal");
const sfWave = $("sf-wave");
const sfCtx2 = sfWave.getContext("2d");

function openSfericDetail(id) {
    fetch(`/api/sferic/${id}/waveform`)
        .then(r => r.json())
        .then(d => {
            if (d.error) { alert("波形未缓存（事件太老或后端已重启）"); return; }
            $("sf-id").textContent = d.id;
            $("sf-t").textContent = d.t !== undefined ? d.t.toFixed(2) : "?";
            const dt_ms = 1000 / d.sample_rate;
            const duration_ms = d.n * dt_ms;

            $("sf-meta").textContent =
                `n=${d.n} samples, sample_rate=${d.sample_rate.toLocaleString()} SPS, ` +
                `duration=${duration_ms.toFixed(2)} ms`;

            // 先让 modal 可见，再测量 canvas 尺寸
            sfModal.classList.add("open");

            // 等浏览器完成 layout
            requestAnimationFrame(() => {
                drawSfericWaveform(d, dt_ms);
            });
        })
        .catch(e => alert("获取波形失败: " + e));
}

function drawSfericWaveform(d, dt_ms) {
    const cssW = sfWave.clientWidth || 580;
    const cssH = sfWave.clientHeight || 220;
    const dpr = window.devicePixelRatio || 1;
    sfWave.width = Math.floor(cssW * dpr);
    sfWave.height = Math.floor(cssH * dpr);
    sfCtx2.setTransform(dpr, 0, 0, dpr, 0, 0);

    const W = cssW;
    const H = cssH;
    const half = H / 2;

    sfCtx2.fillStyle = "#08090c";
    sfCtx2.fillRect(0, 0, W, H);

    // 中线
    sfCtx2.strokeStyle = "#1f2330";
    sfCtx2.lineWidth = 1;
    sfCtx2.beginPath(); sfCtx2.moveTo(0, half); sfCtx2.lineTo(W, half); sfCtx2.stroke();

    // 找峰值位置
    let peakIdx = 0;
    let peakVal = 0;
    const mags = new Float32Array(d.n);
    for (let i = 0; i < d.n; i++) {
        const m = Math.sqrt(d.i[i]*d.i[i] + d.q[i]*d.q[i]);
        mags[i] = m;
        if (m > peakVal) { peakVal = m; peakIdx = i; }
    }
    const maxAbs = Math.max(peakVal, 1e-9);
    const xScale = W / (d.n - 1);
    const yScale = (half * 0.85) / maxAbs;

    // 峰值垂直虚线
    sfCtx2.strokeStyle = "#ffcc00";
    sfCtx2.lineWidth = 1;
    sfCtx2.setLineDash([4, 4]);
    sfCtx2.beginPath();
    sfCtx2.moveTo(peakIdx * xScale, 0);
    sfCtx2.lineTo(peakIdx * xScale, H);
    sfCtx2.stroke();
    sfCtx2.setLineDash([]);

    // I (上半绿)
    sfCtx2.strokeStyle = "#00dd77";
    sfCtx2.lineWidth = 1;
    sfCtx2.beginPath();
    for (let i = 0; i < d.n; i++) {
        const x = i * xScale;
        const y = half / 2 - d.i[i] * yScale;
        if (i === 0) sfCtx2.moveTo(x, y); else sfCtx2.lineTo(x, y);
    }
    sfCtx2.stroke();

    // Q (上半红)
    sfCtx2.strokeStyle = "#dd3355";
    sfCtx2.beginPath();
    for (let i = 0; i < d.n; i++) {
        const x = i * xScale;
        const y = half / 2 - d.q[i] * yScale;
        if (i === 0) sfCtx2.moveTo(x, y); else sfCtx2.lineTo(x, y);
    }
    sfCtx2.stroke();

    // Magnitude (下半黄)
    sfCtx2.strokeStyle = "#ffcc00";
    sfCtx2.lineWidth = 1.4;
    sfCtx2.beginPath();
    for (let i = 0; i < d.n; i++) {
        const x = i * xScale;
        const y = H - mags[i] * yScale * 0.9 - 8;
        if (i === 0) sfCtx2.moveTo(x, y); else sfCtx2.lineTo(x, y);
    }
    sfCtx2.stroke();

    // 峰值标记文字
    sfCtx2.fillStyle = "#ffcc00";
    sfCtx2.font = "10px monospace";
    sfCtx2.fillText(`peak=${peakVal.toFixed(3)} @ ${(peakIdx * dt_ms).toFixed(2)} ms`,
                    Math.min(peakIdx * xScale + 6, W - 200), 14);

    // 标签
    sfCtx2.fillStyle = "#888";
    sfCtx2.font = "10px monospace";
    sfCtx2.fillText("I (绿) / Q (红) 时域", 5, 12);
    sfCtx2.fillText("|IQ| (黄) 包络", 5, half + 12);
    // 时间轴
    sfCtx2.fillText(`0 ms`, 4, H - 4);
    sfCtx2.fillText(`${(d.n * dt_ms).toFixed(2)} ms`, W - 38, H - 4);
}

function closeSfericDetail() {
    sfModal.classList.remove("open");
}

$("sf-close").addEventListener("click", closeSfericDetail);
sfModal.addEventListener("click", (e) => {
    if (e.target === sfModal) closeSfericDetail();
});

// === 卡片折叠/展开 ===
const CARD_ROW_SIZE = {
    waterfall: 'minmax(80px, 1fr)',
    spectrum:  'minmax(80px, 1fr)',
    env:       '130px',
    sferic:    'minmax(100px, 220px)',
};
const CARD_ORDER = ['waterfall', 'spectrum', 'env', 'sferic'];

function updateDisplayLayout() {
    const rows = CARD_ORDER.map(name => {
        const card = document.getElementById(`card-${name}`);
        return card && card.classList.contains('collapsed') ? 'auto' : CARD_ROW_SIZE[name];
    });
    const display = document.querySelector('.display');
    if (display) display.style.gridTemplateRows = rows.join(' ');
}

function toggleCard(name, action) {
    const card = document.getElementById(`card-${name}`);
    if (!card) return;
    if (action === "collapse") {
        card.classList.toggle("collapsed");
        updateDisplayLayout();
        requestAnimationFrame(resizeAll);
    } else if (action === "expand") {
        // 移除所有 expanded，再给自己加
        document.querySelectorAll(".card.expanded").forEach(c => c.classList.remove("expanded"));
        card.classList.toggle("expanded");
    }
}

document.querySelectorAll(".card-btn").forEach(b => {
    b.addEventListener("click", () => {
        toggleCard(b.dataset.card, b.dataset.action);
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

// === Overflow 警告 ===
let overflowWarned = false;
let overflowDismissed = false;
const warnBanner = $("warn-banner");
$("warn-dismiss").addEventListener("click", () => {
    warnBanner.style.display = "none";
    overflowDismissed = true;
});
function checkOverflow(peak, f) {
    if (overflowDismissed) return;
    if (peak > 0.95) {
        if (!overflowWarned) {
            warnBanner.style.display = "flex";
            $("warn-text").textContent =
                `⚠ ADC 过载：|IQ| 滑动平均峰值 = ${peak.toFixed(3)} (>0.95)。` +
                `请降低增益 (RFGR↑ 或 IFGR↓) 以避免信号失真。`;
            overflowWarned = true;
        } else {
            $("warn-text").textContent =
                `⚠ ADC 过载持续：peak=${peak.toFixed(3)}。请降低增益。`;
        }
    } else if (peak < 0.7 && overflowWarned) {
        // 信号降下来后自动隐藏
        warnBanner.style.display = "none";
        overflowWarned = false;
    }
}

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
    $("ctl-freq").value = formatFreq(s.center_freq);
    $("ctl-sr").value = s.sample_rate;
    $("ctl-ifgr").value = s.ifgr;
    $("val-ifgr").textContent = s.ifgr;
    $("ctl-rfgr").value = s.rfgr;
    $("val-rfgr").textContent = s.rfgr;
    $("ctl-thresh").value = s.sferic_thresh_db;
    $("ctl-cooldown").value = s.sferic_cooldown;
    $("sample-rate").textContent = `SR: ${(s.sample_rate/1e6).toFixed(2)} MSPS`;
});

socket.on("frame", (f) => {
    pendingFrame = f;
});

socket.on("sferic", (ev) => {
    pendingSferics.push(ev);
});

// === UI 控制 ===
function applyControls() {
    const msg = $("ctl-msg");
    const freqHz = parseFreq($("ctl-freq").value);
    if (!isFinite(freqHz) || freqHz <= 0) {
        msg.textContent = "✗ 频率格式无效 (示例: 50M, 24000, 24k)";
        msg.className = "hint err";
        return;
    }
    const payload = {
        center_freq: freqHz,
        sample_rate: parseFloat($("ctl-sr").value),
        ifgr: parseInt($("ctl-ifgr").value),
        rfgr: parseInt($("ctl-rfgr").value),
        sferic_thresh_db: parseFloat($("ctl-thresh").value),
        sferic_cooldown: parseFloat($("ctl-cooldown").value),
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
            msg.textContent = `✓ 已应用 → ${formatFreq(payload.center_freq)}`;
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

$("apply-btn").addEventListener("click", applyControls);
$("ctl-ifgr").addEventListener("input", e => { $("val-ifgr").textContent = e.target.value; });
$("ctl-rfgr").addEventListener("input", e => { $("val-rfgr").textContent = e.target.value; });

document.querySelectorAll(".preset").forEach(b => {
    b.addEventListener("click", () => {
        // 只改中心频率，不动采样率（用户自行决定 SR）
        const hz = parseFloat(b.dataset.freq);
        $("ctl-freq").value = formatFreq(hz);
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
    $("ctl-freq").value = formatFreq(s.center_freq);
    $("ctl-sr").value = s.sample_rate;
    $("ctl-ifgr").value = s.ifgr;
    $("val-ifgr").textContent = s.ifgr;
    $("ctl-rfgr").value = s.rfgr;
    $("val-rfgr").textContent = s.rfgr;
    $("ctl-thresh").value = s.sferic_thresh_db;
    $("ctl-cooldown").value = s.sferic_cooldown;
    $("sample-rate").textContent = `SR: ${(s.sample_rate/1e6).toFixed(2)} MSPS`;
});