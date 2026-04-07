/* global state */
let wsConnected = false;
let snapshotFps = null;
const MAX_HISTORY = 50;

const $ = (id) => document.getElementById(id);

const camBadge   = $("cam-status");
const wsBadge    = $("ws-status");
const frameBadge = $("frame-badge");
const mjpeg      = $("mjpeg");
const detCanvas  = $("det-overlay");
const detCtx     = detCanvas ? detCanvas.getContext("2d") : null;
let latestBoxes  = [];

/* drive mode state */
const modeInteractiveBtn = $("mode-interactive");
const modeVlmBtn = $("mode-vlm");
const driveStatusEl = $("drive-status");
const manualPad = $("manual-pad");
const driveBtns = Array.from(document.querySelectorAll(".drive-btn[data-action]"));
let driveMode = "interactive";
let driveAvailable = false;
let activeHoldAction = null;

/* ── WebSocket ─────────────────────────────────────────────────────────────── */
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/results`);

  ws.onopen = () => {
    wsConnected = true;
    wsBadge.textContent = "WebSocket  ●";
    wsBadge.className = "badge ok";
  };

  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (data._ping) return;
    renderResult(data);
  };

  ws.onclose = () => {
    wsConnected = false;
    wsBadge.textContent = "WebSocket  ○";
    wsBadge.className = "badge err";
    setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    wsBadge.textContent = "WebSocket  ✕";
    wsBadge.className = "badge err";
  };
}

/* ── Detections overlay WS ────────────────────────────────────────────────── */
function connectDetectionsWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/detections`);

  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    latestBoxes = Array.isArray(data.boxes) ? data.boxes : [];
    drawDetections();
  };

  ws.onclose = () => {
    setTimeout(connectDetectionsWS, 1500);
  };

  ws.onerror = () => {
    ws.close();
  };
}

/* ── Drive mode APIs ──────────────────────────────────────────────────────── */
async function postJson(url, payload) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data && data.detail) detail = String(data.detail);
    } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

async function refreshDriveStatus() {
  try {
    const resp = await fetch("/api/drive/status");
    const data = await resp.json();
    renderDriveStatus(data);
  } catch (err) {
    renderDriveStatus({
      available: false,
      mode: "interactive",
      error: String(err),
      vlm_running: false,
    });
  }
}

function renderDriveStatus(data) {
  driveMode = data.mode || "interactive";
  driveAvailable = Boolean(data.available);
  const vlmRunning = Boolean(data.vlm_running);

  modeInteractiveBtn.classList.toggle("active", driveMode === "interactive");
  modeVlmBtn.classList.toggle("active", driveMode === "vlm");

  modeInteractiveBtn.disabled = !driveAvailable;
  modeVlmBtn.disabled = !driveAvailable;

  const interactiveEnabled = driveAvailable && driveMode === "interactive";
  manualPad.classList.toggle("disabled", !interactiveEnabled);
  for (const btn of driveBtns) {
    btn.disabled = !interactiveEnabled;
  }

  if (!driveAvailable) {
    driveStatusEl.textContent = `Drive unavailable: ${data.error || "unknown error"}`;
    return;
  }

  if (driveMode === "vlm") {
    driveStatusEl.textContent = vlmRunning
      ? "Drive mode: VLM auto cruise running"
      : "Drive mode: VLM selected, waiting for controller";
    return;
  }

  driveStatusEl.textContent = "Drive mode: interactive manual control";
}

async function switchDriveMode(mode) {
  try {
    const data = await postJson("/api/drive/mode", { mode });
    activeHoldAction = null;
    renderDriveStatus(data);
  } catch (err) {
    driveStatusEl.textContent = `Mode switch failed: ${String(err)}`;
  }
}

async function sendManualAction(action) {
  if (!driveAvailable || driveMode !== "interactive") return;
  try {
    const data = await postJson("/api/drive/manual", { action });
    renderDriveStatus(data);
  } catch (err) {
    driveStatusEl.textContent = `Manual control failed: ${String(err)}`;
  }
}

function normalizeKeyAction(key) {
  const k = key.toLowerCase();
  if (k === "w" || key === "ArrowUp") return "forward";
  if (k === "s" || key === "ArrowDown") return "back";
  if (k === "a" || key === "ArrowLeft") return "left";
  if (k === "d" || key === "ArrowRight") return "right";
  if (key === " ") return "stop";
  return null;
}

function bindDriveControls() {
  modeInteractiveBtn.addEventListener("click", () => switchDriveMode("interactive"));
  modeVlmBtn.addEventListener("click", () => switchDriveMode("vlm"));

  for (const btn of driveBtns) {
    const action = btn.dataset.action;
    if (!action) continue;

    btn.addEventListener("pointerdown", () => {
      activeHoldAction = action;
      sendManualAction(action);
    });

    const release = () => {
      if (activeHoldAction !== null) {
        activeHoldAction = null;
        sendManualAction("stop");
      }
    };

    btn.addEventListener("pointerup", release);
    btn.addEventListener("pointercancel", release);
    btn.addEventListener("pointerleave", release);
  }

  window.addEventListener("keydown", (ev) => {
    const action = normalizeKeyAction(ev.key);
    if (!action) return;

    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", " "].includes(ev.key)) {
      ev.preventDefault();
    }

    if (driveMode !== "interactive" || !driveAvailable) return;
    if (ev.repeat && action !== "stop") return;

    if (action === "stop") {
      activeHoldAction = null;
      sendManualAction("stop");
      return;
    }

    activeHoldAction = action;
    sendManualAction(action);
  });

  window.addEventListener("keyup", (ev) => {
    const action = normalizeKeyAction(ev.key);
    if (!action || action === "stop") return;
    if (driveMode !== "interactive" || !driveAvailable) return;

    if (activeHoldAction === action) {
      activeHoldAction = null;
      sendManualAction("stop");
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && driveMode === "interactive") {
      activeHoldAction = null;
      sendManualAction("stop");
    }
  });
}

/* ── Render result ─────────────────────────────────────────────────────────── */
function catClass(cat) {
  if (!cat) return "";
  const v = cat.trim().toUpperCase();
  if (v === "GO")      return "go";
  if (v === "CAUTION") return "caution";
  if (v === "NO-GO")   return "nogo";
  return "";
}

function renderResult(data) {
  /* timestamp */
  const ts = data._inferred_at ? new Date(data._inferred_at * 1000) : new Date();
  const timeText = ts.toLocaleTimeString();
  const headerTime = $("last-inferred-header");
  const panelTime = $("last-inferred-panel");
  if (headerTime) headerTime.textContent = "Recent Inference: " + timeText;
  if (panelTime) panelTime.textContent = timeText;

  /* frame badge */
  if (data._frame_count !== undefined) {
    const fpsLabel = snapshotFps !== null ? ` @ ${snapshotFps} FPS` : "";
    frameBadge.textContent = `${data._frame_count} frames${fpsLabel}`;
    frameBadge.classList.remove("hidden");
  }

  /* ── Current card ── */
  const errEl    = $("inf-error");
  const catEl    = $("inf-cat");
  const spaceEl  = $("inf-space");
  const hazEl    = $("inf-hazards");
  const actionEl = $("inf-action");
  const reasonEl = $("inf-reason");

  if (data.error) {
    catEl.textContent    = "ERR";
    catEl.className      = "inf-cat";
    spaceEl.textContent  = "";
    if (hazEl) hazEl.textContent = "—";
    if (hazEl) hazEl.className = "inf-value";
    actionEl.textContent = "—";
    actionEl.className   = "inf-value";
    reasonEl.textContent = "—";
    errEl.textContent    = "⚠ " + escHtml(data.error);
    errEl.classList.remove("hidden");
  } else {
    errEl.classList.add("hidden");
    const status = data.status || {};
    const cat    = status.cat   || "—";
    const space  = status.free_space != null ? status.free_space + " cm" : "—";
    const hazards = status.hazards || "none";
    const cls    = catClass(cat);

    catEl.textContent    = cat;
    catEl.className      = "inf-cat" + (cls ? " " + cls : "");
    spaceEl.textContent  = "Free space: " + space;
    if (hazEl) hazEl.textContent = hazards;
    if (hazEl) hazEl.className = "inf-value" + (cls ? " " + cls : "");
    actionEl.textContent = data.action || "—";
    actionEl.className   = "inf-value" + (cls ? " " + cls : "");
    reasonEl.textContent = data.analysis || "—";
  }

  /* ── Append to history ── */
  appendHistory(data, ts);
}

/* ── History log ───────────────────────────────────────────────────────────── */
const histList = $("hist-list");

function appendHistory(data, ts) {
  /* trim old entries */
  while (histList.children.length >= MAX_HISTORY) {
    histList.removeChild(histList.lastChild);
  }

  const row = document.createElement("div");
  row.className = "hist-row";

  let catText = "ERR", cls = "err", action = "", reason = "";
  let hazards = "—";
  if (!data.error) {
    const status = data.status || {};
    catText = status.cat || "—";
    cls     = catClass(catText);
    hazards = status.hazards || "none";
    action  = data.action || "";
    reason  = data.analysis || "";
  } else {
    action = data.error;
  }

  const space = (data.status && data.status.free_space != null)
    ? ` · ${data.status.free_space} cm` : "";

  row.innerHTML = `
    <div class="hist-cat ${cls}">${escHtml(catText)}</div>
    <div class="hist-detail">
      <span class="hist-time">${ts.toLocaleTimeString()}${escHtml(space)}</span>
      <span class="hist-hazards">Hazards: ${escHtml(hazards)}</span>
      <span class="hist-action">${escHtml(action)}</span>
      <span class="hist-reason">${escHtml(reason)}</span>
    </div>`;

  histList.prepend(row);
}

function clearHistory() {
  histList.innerHTML = "";
}

/* ── Camera status polling ─────────────────────────────────────────────────── */
function pollStatus() {
  fetch("/api/status")
    .then((r) => r.json())
    .then((d) => {
      if (d.camera_ready) {
        camBadge.textContent = "Camera  ●";
        camBadge.className = "badge ok";
        $("cam-overlay").classList.add("hidden");
      } else {
        camBadge.textContent = "Camera  ○";
        camBadge.className = "badge err";
        $("cam-overlay").classList.remove("hidden");
        clearDetections();
      }
    })
    .catch(() => {
      camBadge.textContent = "Camera  —";
      camBadge.className = "badge";
    });
}

function clearDetections() {
  latestBoxes = [];
  drawDetections();
}

function resizeDetectionsCanvas() {
  if (!detCanvas || !mjpeg) return;
  const w = mjpeg.clientWidth || 0;
  const h = mjpeg.clientHeight || 0;
  if (w <= 0 || h <= 0) return;

  // Match the canvas box to the actual displayed MJPEG box (not the whole wrapper).
  detCanvas.style.left = `${mjpeg.offsetLeft}px`;
  detCanvas.style.top = `${mjpeg.offsetTop}px`;
  detCanvas.style.width = `${w}px`;
  detCanvas.style.height = `${h}px`;

  const dpr = window.devicePixelRatio || 1;
  const targetW = Math.max(1, Math.round(w * dpr));
  const targetH = Math.max(1, Math.round(h * dpr));
  if (detCanvas.width !== targetW) detCanvas.width = targetW;
  if (detCanvas.height !== targetH) detCanvas.height = targetH;

  // Draw in CSS pixels while the backing store is scaled for retina displays.
  detCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawDetections() {
  if (!detCanvas || !detCtx) return;
  resizeDetectionsCanvas();

  const w = mjpeg.clientWidth || 0;
  const h = mjpeg.clientHeight || 0;
  if (w <= 0 || h <= 0) return;
  detCtx.clearRect(0, 0, w, h);
  if (!latestBoxes.length) return;

  detCtx.lineWidth = 2;
  detCtx.font = "13px system-ui, sans-serif";

  for (const b of latestBoxes) {
    const x1 = Math.max(0, Math.min(w, b.x1 * w));
    const y1 = Math.max(0, Math.min(h, b.y1 * h));
    const x2 = Math.max(0, Math.min(w, b.x2 * w));
    const y2 = Math.max(0, Math.min(h, b.y2 * h));
    const bw = Math.max(1, x2 - x1);
    const bh = Math.max(1, y2 - y1);

    detCtx.strokeStyle = "#20d36b";
    detCtx.strokeRect(x1, y1, bw, bh);

    const label = `${b.label || "obj"} ${(Number(b.conf) || 0).toFixed(2)}`;
    const tw = detCtx.measureText(label).width;
    const ty = Math.max(14, y1 - 6);
    detCtx.fillStyle = "rgba(32, 211, 107, 0.95)";
    detCtx.fillRect(x1, ty - 14, tw + 10, 16);
    detCtx.fillStyle = "#08140d";
    detCtx.fillText(label, x1 + 5, ty - 2);
  }
}

/* ── MJPEG error / reconnect ───────────────────────────────────────────────── */
mjpeg.addEventListener("error", () => {
  setTimeout(() => {
    mjpeg.src = `/stream.mjpeg?_=${Date.now()}`;
  }, 2000);
});

mjpeg.addEventListener("load", () => {
  resizeDetectionsCanvas();
  drawDetections();
});

window.addEventListener("resize", () => {
  resizeDetectionsCanvas();
  drawDetections();
});

/* ── Helpers ───────────────────────────────────────────────────────────────── */
function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/* ── Boot ──────────────────────────────────────────────────────────────────── */
fetch("/api/config")
  .then((r) => r.json())
  .then((cfg) => { snapshotFps = cfg.snapshot_fps; })
  .catch(() => {});

connectWS();
connectDetectionsWS();
bindDriveControls();
refreshDriveStatus();
pollStatus();
setInterval(pollStatus, 5000);
setInterval(refreshDriveStatus, 2500);
