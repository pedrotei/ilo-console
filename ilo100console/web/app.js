"use strict";

// Same dual-transport pattern as ilo3console/web/app.js:
//   • Native desktop (pywebview): Python connects directly, calls
//     window.__onFrame / window.__onEvent; input goes to window.pywebview.api.
//   • Browser: a WebSocket bridge relays frames and input.

function isNative() { return !!(window.pywebview && window.pywebview.api); }

const $ = (id) => document.getElementById(id);

const ui = {
  overlay: $("overlay"), form: $("connect-form"), err: $("err"),
  target: $("target"), canvas: $("screen"), placeholder: $("placeholder"),
  lampLink: $("lamp-link"), lampPower: $("lamp-power"), lampKbd: $("lamp-kbd"),
  lampVideo: $("lamp-video"), lampMouse: $("lamp-mouse"),
  fRes: $("f-res"), fFps: $("f-fps"), fMode: $("f-mode"), fStatus: $("f-status"),
  btnCad: $("btn-cad"), btnRefresh: $("btn-refresh"), btnDisconnect: $("btn-disconnect"),
  btnPower: $("btn-power"), powerMenu: $("power-menu"),
  powerModal: $("power-modal"), powerModalText: $("power-modal-text"),
  powerCancel: $("power-cancel"), powerConfirm: $("power-confirm"),
  btnMedia: $("btn-media"), mediaModal: $("media-modal"),
  mediaType: $("media-type"), mediaBrowse: $("media-browse"),
  mediaPath: $("media-path"), mediaStatus: $("media-status"),
  mediaMount: $("media-mount"), mediaUnmount: $("media-unmount"),
  mediaClose: $("media-close"),
  connectBtn: document.querySelector("#connect-form button.connect"),
  connectLabel: document.querySelector("#connect-form button.connect .label"),
};

const ctx = ui.canvas.getContext("2d");
let ws = null;
let fbW = 0, fbH = 0;
let lastFrameAt = 0, fpsEMA = 0;

// When the chassis is powered off, the server has no video signal to send
// and just stops updating -- so the canvas is left showing whatever was on
// screen at the moment of shutdown, cursor included, which reads as if the
// system were still live. Blank it and ignore further frames until power
// comes back on.
let poweredOff = false;
function setPoweredOff(off) {
  poweredOff = off;
  if (off) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, ui.canvas.width, ui.canvas.height);
    ui.placeholder.textContent = "system powered off";
    ui.placeholder.style.display = "";
  } else if (fbW && fbH) {
    ui.placeholder.style.display = "none";
  }
}

// -- prefill ------------------------------------------------------------

function applyPrefill(host, user) {
  if (host) $("host").value = host;
  if (user) $("username").value = user;
  (host ? (user ? $("password") : $("username")) : $("host")).focus();
}
window.addEventListener("pywebviewready", () => {
  window.pywebview.api.get_prefill().then((p) => applyPrefill(p && p.host, p && p.user));
}, { once: true });
if (!isNative()) {
  const q = new URLSearchParams(location.search);
  applyPrefill(q.get("host"), q.get("user"));
}

// -- UI state -------------------------------------------------------------

function lampClass(el, cls) {
  el.classList.remove("on", "off");
  if (cls) el.classList.add(cls);
}
function setControls(connected) {
  for (const b of [ui.btnCad, ui.btnRefresh, ui.btnDisconnect, ui.btnPower, ui.btnMedia]) {
    b.disabled = !connected;
  }
  if (!connected) { ui.powerMenu.hidden = true; ui.mediaModal.hidden = true; }
}
function setConnecting(busy) {
  ui.connectBtn.classList.toggle("busy", busy);
  ui.connectBtn.disabled = busy;
  ui.connectLabel.textContent = busy ? "Connecting…" : "Open console";
  for (const el of ui.form.querySelectorAll("input")) el.disabled = busy;
}
function onReady(host, privileges) {
  setConnecting(false);
  ui.overlay.hidden = true;
  ui.target.textContent = host;
  lampClass(ui.lampLink, "on");
  poweredOff = false; // reset from any prior session; the power event (below) sets it correctly
  if (privileges) {
    lampClass(ui.lampKbd, privileges.keyboard ? "on" : "off");
    lampClass(ui.lampVideo, privileges.video ? "on" : "off");
    lampClass(ui.lampMouse, privileges.mouse ? "on" : "off");
  }
  setControls(true);
  ui.fStatus.textContent = "connected";
  ui.canvas.focus();
}
function onConnectError(text) {
  setConnecting(false);
  ui.err.textContent = text || "connection failed";
}
function onLinkLost(reason) {
  lampClass(ui.lampLink, null);
  lampClass(ui.lampPower, null);
  lampClass(ui.lampKbd, null);
  lampClass(ui.lampVideo, null);
  lampClass(ui.lampMouse, null);
  setControls(false);
  if (!ui.overlay.hidden) {
    setConnecting(false);
    if (!ui.err.textContent) ui.err.textContent = reason || "connection closed";
  } else {
    ui.fStatus.textContent = reason || "disconnected";
    ui.overlay.hidden = false;
  }
}

// -- connect ----------------------------------------------------------------

ui.form.addEventListener("submit", (e) => {
  e.preventDefault();
  ui.err.textContent = "";
  setConnecting(true);
  const params = {
    host: $("host").value.trim(),
    username: $("username").value,
    password: $("password").value,
    port: Number($("port").value || 80),
    ssl: $("ssl").checked,
  };
  if (isNative()) connectNative(params);
  else connectWebSocket(params);
});

function connectNative(params) {
  window.pywebview.api.connect(params).then((res) => {
    if (res && res.ok) onReady(res.host, res.privileges);
    else onConnectError(res && res.error);
  }).catch((err) => onConnectError(String(err)));
}

function connectWebSocket(params) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ t: "connect", ...params }));
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) drawFromArrayBuffer(ev.data);
    else { try { handleEvent(JSON.parse(ev.data)); } catch {} }
  };
  ws.onclose = () => onLinkLost("connection closed");
  ws.onerror = () => onConnectError("connection failed");
}

// -- transport-agnostic input send ------------------------------------------

function send(obj) {
  if (isNative()) window.pywebview.api.input(JSON.stringify(obj));
  else if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

ui.btnDisconnect.addEventListener("click", () => {
  if (isNative()) { window.pywebview.api.disconnect(); location.reload(); }
  else if (ws) ws.close();
});
ui.btnRefresh.addEventListener("click", () => send({ t: "refresh" }));

// -- power control (separate HTTP/CGI channel on the iLO, not the KVM
// socket -- see power.py) and the shared confirm modal for it + Ctrl+Alt+Del
// ----------------------------------------------------------------------
//
// Both send input the device can't undo -- a power action can force the OS
// off mid-write, and Ctrl+Alt+Del reboots most OSes outright -- so neither
// fires straight from its button; both go through this one modal.

let pendingAction = null;

function closePowerMenu() { ui.powerMenu.hidden = true; }
function closeConfirmModal() { ui.powerModal.hidden = true; pendingAction = null; }
function confirmAction(text, action) {
  pendingAction = action;
  ui.powerModalText.textContent = text;
  ui.powerModal.hidden = false;
  ui.powerConfirm.focus();
}

ui.btnCad.addEventListener("click", () => {
  confirmAction(
    "Send Ctrl+Alt+Delete to the server? Most operating systems reboot immediately on this.",
    () => send({ t: "cad" })
  );
});

const POWER_PROMPT = {
  PowerUp: "Power on the server?",
  PowerCycle: "Power cycle — cut power and immediately power back on?",
  SoftPwrDn: "Send a soft (ACPI) shutdown request to the OS?",
  HardReset: "Issue a hard system reset?",
  PowerDown: "Power off (hard) — cut power immediately, without a clean OS shutdown?",
};

function sendPower(action) {
  if (isNative()) {
    window.pywebview.api.power(action).then((res) => {
      if (res && res.ok) {
        lampClass(ui.lampPower, res.status === "ON" ? "on" : "off");
        setPoweredOff(res.status !== "ON");
      } else {
        ui.fStatus.textContent = (res && res.error) || "power action failed";
      }
    });
  } else {
    send({ t: "power", action });
  }
}

ui.btnPower.addEventListener("click", (e) => {
  e.stopPropagation();
  ui.powerMenu.hidden = !ui.powerMenu.hidden;
});
for (const opt of ui.powerMenu.querySelectorAll("button[data-power]")) {
  opt.addEventListener("click", () => {
    const action = opt.dataset.power;
    closePowerMenu();
    confirmAction(POWER_PROMPT[action] || "Confirm this action.", () => sendPower(action));
  });
}
ui.powerCancel.addEventListener("click", closeConfirmModal);
ui.powerConfirm.addEventListener("click", () => {
  if (pendingAction) pendingAction();
  closeConfirmModal();
});
ui.powerModal.addEventListener("click", (e) => {
  if (e.target === ui.powerModal) closeConfirmModal();  // click backdrop = cancel
});
document.addEventListener("click", closePowerMenu);   // click elsewhere closes menu
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closePowerMenu(); if (!ui.powerModal.hidden) closeConfirmModal(); }
});

// -- virtual media ------------------------------------------------------
//
// Local-file mounting is native-only (Python opens a file on this machine
// and serves it to the iLO) -- not available over the WebSocket bridge.
// Experimental: see docs/PROTOCOL_ILO100.md's "Virtual media" section.

let pickedMediaPath = null;

ui.btnMedia.addEventListener("click", () => { ui.mediaModal.hidden = false; });
ui.mediaClose.addEventListener("click", () => { ui.mediaModal.hidden = true; });
ui.mediaModal.addEventListener("click", (e) => {
  if (e.target === ui.mediaModal) ui.mediaModal.hidden = true;
});

ui.mediaBrowse.addEventListener("click", () => {
  if (!isNative()) return;
  window.pywebview.api.vm_pick_file().then((res) => {
    if (res && res.ok) {
      pickedMediaPath = res.path;
      ui.mediaPath.textContent = res.path;
      ui.mediaMount.disabled = false;
    } else if (res && res.error) {
      ui.mediaStatus.textContent = res.error;
      ui.mediaStatus.className = "media-status err";
    }
  });
});

ui.mediaMount.addEventListener("click", () => {
  if (!isNative() || !pickedMediaPath) return;
  const mediaType = Number(ui.mediaType.value);
  ui.mediaStatus.textContent = "mounting…";
  ui.mediaStatus.className = "media-status";
  window.pywebview.api.vm_mount(pickedMediaPath, mediaType).then((res) => {
    if (res && res.ok) {
      ui.mediaStatus.textContent = "mounted";
      ui.mediaStatus.className = "media-status ok";
      ui.mediaUnmount.disabled = false;
    } else {
      ui.mediaStatus.textContent = (res && res.error) || "mount failed";
      ui.mediaStatus.className = "media-status err";
    }
  });
});

ui.mediaUnmount.addEventListener("click", () => {
  if (!isNative()) return;
  window.pywebview.api.vm_unmount().then((res) => {
    if (res && res.ok) {
      ui.mediaStatus.textContent = "not mounted";
      ui.mediaStatus.className = "media-status";
      ui.mediaUnmount.disabled = true;
    } else {
      ui.mediaStatus.textContent = (res && res.error) || "unmount failed";
      ui.mediaStatus.className = "media-status err";
    }
  });
});

// -- inbound events -----------------------------------------------------

window.__onFrame = (b64) => drawFromBase64(b64);
window.__onEvent = (json) => { try { handleEvent(JSON.parse(json)); } catch {} };

function handleEvent(msg) {
  switch (msg.t) {
    case "ready": onReady(msg.host, msg.privileges); break;      // WebSocket-only
    case "error": onConnectError(msg.msg); break;                 // WebSocket-only
    case "resize":
      fbW = msg.w; fbH = msg.h;
      ui.canvas.width = fbW; ui.canvas.height = fbH;
      ui.fRes.textContent = `${fbW}×${fbH}`;
      break;
    case "key_indicators": break; // available if a caps/num/scroll indicator is added later
    case "control":
      // The server defaults every connection to view-only; the client auto-
      // requests full control on connect (see client.py), but the grant
      // isn't instant -- surface the wait instead of leaving input silently
      // inert with no explanation.
      ui.fStatus.textContent = msg.has_control ? "connected" : "connected (requesting control…)";
      break;
    case "power":
      lampClass(ui.lampPower, msg.status === "ON" ? "on" : "off");
      setPoweredOff(msg.status !== "ON");
      break;
    case "power_error": ui.fStatus.textContent = msg.msg || "power action failed"; break; // WebSocket-only
    case "disconnected": onLinkLost(msg.reason || "link to iLO dropped"); break;
  }
}

// -- frame rendering --------------------------------------------------------

async function drawFromArrayBuffer(buf) {
  if (poweredOff) return; // stale frame from before shutdown -- see setPoweredOff
  const bmp = await createImageBitmap(new Blob([buf], { type: "image/png" }));
  paint(bmp, bmp.width, bmp.height);
  bmp.close();
}
async function drawFromBase64(b64) {
  if (poweredOff) return; // stale frame from before shutdown -- see setPoweredOff
  const img = new Image();
  img.src = "data:image/png;base64," + b64;
  await img.decode();
  paint(img, img.naturalWidth, img.naturalHeight);
}
function paint(source, w, h) {
  if (poweredOff) return; // guard against a frame that raced the power event
  ui.placeholder.style.display = "none";
  if (w !== ui.canvas.width || h !== ui.canvas.height) {
    ui.canvas.width = w; ui.canvas.height = h;
    fbW = w; fbH = h;
    ui.fRes.textContent = `${w}×${h}`;
  }
  ctx.drawImage(source, 0, 0);
  const now = performance.now();
  if (lastFrameAt) {
    const inst = 1000 / (now - lastFrameAt);
    fpsEMA = fpsEMA ? fpsEMA * 0.8 + inst * 0.2 : inst;
    ui.fFps.textContent = fpsEMA.toFixed(0);
  }
  lastFrameAt = now;
  // Heuristic: text-mode screens are always exactly N*8 x M*16 (80x25 = 640x400
  // is by far the common case); good enough for a display-only hint.
  ui.fMode.textContent = (h % 16 === 0 && w % 8 === 0) ? "text" : "graphics";
}

// -- keyboard -----------------------------------------------------------
//
// KeyStateChange takes one HID usage id per event (no combined report like
// iLO 3), so we just forward the browser's own KeyboardEvent.code string and
// let the Python side (keymap.py) resolve it — the mapping table only needs
// to live in one place that way.

ui.canvas.addEventListener("keydown", (e) => {
  e.preventDefault();
  send({ t: "key", code: e.code, down: true });
});
ui.canvas.addEventListener("keyup", (e) => {
  e.preventDefault();
  send({ t: "key", code: e.code, down: false });
});
ui.canvas.addEventListener("blur", () => send({ t: "keyup" }));

// -- mouse ------------------------------------------------------------------
//
// Absolute mode only: iLO 100's mouse protocol sends raw framebuffer pixel
// coordinates with no scaling (see docs/PROTOCOL_ILO100.md).

function toFb(e) {
  const r = ui.canvas.getBoundingClientRect();
  const x = Math.round((e.clientX - r.left) / r.width * (fbW || ui.canvas.width));
  const y = Math.round((e.clientY - r.top) / r.height * (fbH || ui.canvas.height));
  return { x: Math.max(0, x), y: Math.max(0, y) };
}
function sendMouse(e) {
  const { x, y } = toFb(e);
  send({
    t: "mouse", x, y,
    left: !!(e.buttons & 1), right: !!(e.buttons & 2), middle: !!(e.buttons & 4),
  });
}
ui.canvas.addEventListener("mousemove", sendMouse);
ui.canvas.addEventListener("mousedown", (e) => { ui.canvas.focus(); sendMouse(e); });
ui.canvas.addEventListener("mouseup", sendMouse);
ui.canvas.addEventListener("contextmenu", (e) => e.preventDefault());
