"use strict";

// The same front-end runs two ways:
//   • Native desktop (pywebview): Python connects to the iLO directly and calls
//     window.__onFrame / window.__onEvent; input goes to window.pywebview.api.
//   • Browser: a WebSocket bridge relays frames and input.
// isNative() picks the path at runtime.

function isNative() { return !!(window.pywebview && window.pywebview.api); }

// Maps DOM KeyboardEvent.code -> USB HID usage id. Modifier keys are handled
// separately (folded into the report's modifier byte), so they are omitted here.
const HID = (() => {
  const m = {};
  const put = (code, id) => { m[code] = id; };
  "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach((c, i) => put("Key" + c, 0x04 + i));
  for (let i = 1; i <= 9; i++) put("Digit" + i, 0x1e + (i - 1));
  put("Digit0", 0x27);
  Object.assign(m, {
    Enter: 0x28, Escape: 0x29, Backspace: 0x2a, Tab: 0x2b, Space: 0x2c,
    Minus: 0x2d, Equal: 0x2e, BracketLeft: 0x2f, BracketRight: 0x30,
    Backslash: 0x31, Semicolon: 0x33, Quote: 0x34, Backquote: 0x35,
    Comma: 0x36, Period: 0x37, Slash: 0x38, CapsLock: 0x39,
    PrintScreen: 0x46, ScrollLock: 0x47, Pause: 0x48, Insert: 0x49,
    Home: 0x4a, PageUp: 0x4b, Delete: 0x4c, End: 0x4d, PageDown: 0x4e,
    ArrowRight: 0x4f, ArrowLeft: 0x50, ArrowDown: 0x51, ArrowUp: 0x52,
    NumLock: 0x53, NumpadDivide: 0x54, NumpadMultiply: 0x55,
    NumpadSubtract: 0x56, NumpadAdd: 0x57, NumpadEnter: 0x58,
    Numpad0: 0x62, NumpadDecimal: 0x63,
  });
  for (let i = 1; i <= 9; i++) put("Numpad" + i, 0x59 + (i - 1));
  for (let i = 1; i <= 12; i++) put("F" + i, 0x3a + (i - 1));
  return m;
})();

const MOD = { LCTRL: 0x01, LSHIFT: 0x02, LALT: 0x04, LGUI: 0x08 };
const MB = { LEFT: 0x02, RIGHT: 0x01, CENTER: 0x04 };

const $ = (id) => document.getElementById(id);

const ui = {
  overlay: $("overlay"), form: $("connect-form"), err: $("err"),
  target: $("target"), canvas: $("screen"), placeholder: $("placeholder"),
  lampLink: $("lamp-link"), lampPower: $("lamp-power"),
  lampHealth: $("lamp-health"), lampEnc: $("lamp-enc"),
  fRes: $("f-res"), fFps: $("f-fps"), fCipher: $("f-cipher"), fStatus: $("f-status"),
  btnCad: $("btn-cad"), btnRefresh: $("btn-refresh"),
  btnDisconnect: $("btn-disconnect"),
  btnPower: $("btn-power"), powerMenu: $("power-menu"),
  powerModal: $("power-modal"), powerModalText: $("power-modal-text"),
  powerCancel: $("power-cancel"), powerConfirm: $("power-confirm"),
  connectBtn: $("connect-btn"), connectLabel: $("connect-btn").querySelector(".label"),
  btnMedia: $("btn-media"), mediaModal: $("media-modal"),
  mediaDevices: $("media-devices"), mediaTemplate: $("media-device-template"),
  mediaClose: $("media-close"),
};

const ctx = ui.canvas.getContext("2d");
let ws = null;
let fbW = 0, fbH = 0;
const pressed = new Set();
let lastFrameAt = 0, fpsEMA = 0;

// -- prefill (native: from Python; browser: from URL query) -----------------

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

// -- UI state ---------------------------------------------------------------

function lampClass(el, cls) {
  el.classList.remove("on", "warn", "fault", "amber");
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
function onReady(host) {
  setConnecting(false);
  ui.overlay.hidden = true;
  ui.target.textContent = host;
  lampClass(ui.lampLink, "on");
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
  lampClass(ui.lampEnc, null);
  lampClass(ui.lampPower, null);
  lampClass(ui.lampHealth, null);
  ui.fCipher.textContent = "—";
  setControls(false);
  if (!ui.overlay.hidden) {
    // Dropped while the dialog was still up (e.g. mid-connect): report there.
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
  };
  if (isNative()) connectNative(params);
  else connectWebSocket(params);
});

function connectNative(params) {
  window.pywebview.api.connect(params).then((res) => {
    if (res && res.ok) onReady(res.host);
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

// -- shared confirmation modal (power actions, Ctrl+Alt+Del) -----------------
//
// Both send input the server can't undo — a power action can force the OS
// off mid-write, and Ctrl+Alt+Del reboots most OSes outright — so neither
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
  0: "Send a momentary power-button press?",
  1: "Press and hold the power button? This forces the server off.",
  2: "Cold boot — power-cycle the server now?",
  3: "Issue a hard system reset?",
};

ui.btnPower.addEventListener("click", (e) => {
  e.stopPropagation();
  ui.powerMenu.hidden = !ui.powerMenu.hidden;
});
for (const opt of ui.powerMenu.querySelectorAll("button[data-power]")) {
  opt.addEventListener("click", () => {
    const option = Number(opt.dataset.power);
    closePowerMenu();
    confirmAction(
      POWER_PROMPT[option] || "Confirm this action.",
      () => send({ t: "power", option })
    );
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

// -- inbound events (both transports feed these) ----------------------------

// Native push hooks, called from Python via evaluate_js.
window.__onFrame = (b64) => drawFromBase64(b64);
window.__onEvent = (json) => { try { handleEvent(JSON.parse(json)); } catch {} };

function handleEvent(msg) {
  switch (msg.t) {
    case "ready": onReady(msg.host); break;      // WebSocket-only
    case "error": onConnectError(msg.msg); break; // WebSocket-only
    case "resize":
      fbW = msg.w; fbH = msg.h;
      ui.canvas.width = fbW; ui.canvas.height = fbH;
      ui.fRes.textContent = `${fbW}×${fbH}`;
      break;
    case "power": lampClass(ui.lampPower, msg.on ? "on" : "fault"); break;
    case "health": lampClass(ui.lampHealth, ["on", "warn", "fault"][msg.level] || null); break;
    case "status":
      if (msg.field === 4) ui.fFps.textContent = msg.text || "—";
      else ui.fStatus.textContent = msg.text || "—";
      break;
    case "cipher": {
      const names = { 0: "off", 1: "RC4-128", 2: "AES-128", 3: "AES-256" };
      ui.fCipher.textContent = names[msg.cipher] ?? "—";
      lampClass(ui.lampEnc, msg.cipher ? "amber" : null);
      break;
    }
    case "seize": ui.fStatus.textContent = "session seized by another user"; break;
    case "disconnected": onLinkLost("link to iLO dropped"); break;
    case "vm_result": case "vm_status": resolvePendingVm(msg); break;
  }
}

// -- frame rendering --------------------------------------------------------

async function drawFromArrayBuffer(buf) {
  const bmp = await createImageBitmap(new Blob([buf], { type: "image/png" }));
  paint(bmp, bmp.width, bmp.height);
  bmp.close();
}
async function drawFromBase64(b64) {
  const img = new Image();
  img.src = "data:image/png;base64," + b64;
  await img.decode();
  paint(img, img.naturalWidth, img.naturalHeight);
}
function paint(source, w, h) {
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
  // The crypto lamp / cipher field are driven by the "cipher" event, not frames.
}

// -- keyboard ---------------------------------------------------------------

function modByte(e) {
  let m = 0;
  if (e.ctrlKey) m |= MOD.LCTRL;
  if (e.shiftKey) m |= MOD.LSHIFT;
  if (e.altKey) m |= MOD.LALT;
  if (e.metaKey) m |= MOD.LGUI;
  return m;
}
function sendKeyReport(e) {
  send({ t: "key", mod: modByte(e), keys: [...pressed].slice(0, 6) });
}
ui.canvas.addEventListener("keydown", (e) => {
  e.preventDefault();
  const id = HID[e.code];
  if (id !== undefined) pressed.add(id);
  sendKeyReport(e);
});
ui.canvas.addEventListener("keyup", (e) => {
  e.preventDefault();
  const id = HID[e.code];
  if (id !== undefined) pressed.delete(id);
  sendKeyReport(e);
});
ui.canvas.addEventListener("blur", () => { pressed.clear(); send({ t: "keyup" }); });

// -- mouse ------------------------------------------------------------------

function toFb(e) {
  const r = ui.canvas.getBoundingClientRect();
  const x = Math.round((e.clientX - r.left) / r.width * (fbW || ui.canvas.width));
  const y = Math.round((e.clientY - r.top) / r.height * (fbH || ui.canvas.height));
  return { x: Math.max(0, x), y: Math.max(0, y) };
}
function buttonBits(e) {
  let b = 0;
  if (e.buttons & 1) b |= MB.LEFT;
  if (e.buttons & 2) b |= MB.RIGHT;
  if (e.buttons & 4) b |= MB.CENTER;
  return b;
}
function sendMouse(e) {
  const { x, y } = toFb(e);
  send({ t: "mouse", x, y, buttons: buttonBits(e), dx: e.movementX | 0, dy: e.movementY | 0 });
}
ui.canvas.addEventListener("mousemove", sendMouse);
ui.canvas.addEventListener("mousedown", (e) => { ui.canvas.focus(); sendMouse(e); });
ui.canvas.addEventListener("mouseup", sendMouse);
ui.canvas.addEventListener("contextmenu", (e) => e.preventDefault());

// -- virtual media panel ------------------------------------------------

const MEDIA_SLOTS = [
  { device: "FLOPPY", kind: "auto", label: "Floppy / USB key" },
  { device: "CDROM", kind: "cdrom", label: "CD / DVD-ROM" },
];
let pendingVmResolvers = [];

function resolvePendingVm(msg) {
  const i = pendingVmResolvers.findIndex((p) => p.match(msg));
  if (i === -1) return;
  const [p] = pendingVmResolvers.splice(i, 1);
  p.resolve(msg);
}

function bridgeVmCall(request, match, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingVmResolvers = pendingVmResolvers.filter((p) => p.resolve !== resolve);
      reject(new Error("timed out"));
    }, timeoutMs);
    pendingVmResolvers.push({
      match,
      resolve: (msg) => { clearTimeout(timer); resolve(msg); },
    });
    ws.send(JSON.stringify(request));
  });
}

function buildMediaPanel() {
  ui.mediaDevices.innerHTML = "";
  for (const slot of MEDIA_SLOTS) {
    const node = ui.mediaTemplate.content.cloneNode(true);
    const root = node.querySelector(".media-device");
    root.dataset.device = slot.device;
    root.querySelector(".media-device-name").textContent = slot.label;
    root.querySelector(".media-row-local").dataset.native = String(isNative());

    const pathEl = root.querySelector(".media-path");
    const urlEl = root.querySelector(".media-url");
    const statusEl = root.querySelector(".media-status");

    const setStatus = (text, cls) => {
      statusEl.textContent = text;
      statusEl.className = "media-status" + (cls ? " " + cls : "");
    };

    root.querySelector(".media-browse").addEventListener("click", async () => {
      const picked = await window.pywebview.api.vm_pick_file();
      if (!picked || !picked.ok) {
        if (picked && picked.error) setStatus(picked.error, "err");
        return;
      }
      pathEl.textContent = picked.path;
      const mounted = await window.pywebview.api.vm_mount(picked.path, slot.kind);
      setStatus(mounted.ok ? "mounted from local file" : (mounted.error || "mount failed"),
        mounted.ok ? "ok" : "err");
    });

    root.querySelector(".media-connect").addEventListener("click", async () => {
      const url = urlEl.value.trim();
      if (!url) return;
      setStatus("connecting…", null);
      try {
        let result;
        if (isNative()) {
          result = await window.pywebview.api.vm_insert_url(slot.device, url);
        } else {
          const msg = await bridgeVmCall(
            { t: "vm_insert_url", device: slot.device, url },
            (m) => m.t === "vm_result" && m.device === slot.device
          );
          result = { ok: msg.result === "Success", result: msg.result };
        }
        setStatus(result.ok ? "connected: " + url : (result.result || result.error || "failed"),
          result.ok ? "ok" : "err");
      } catch (err) {
        setStatus(String(err), "err");
      }
    });

    root.querySelector(".media-eject").addEventListener("click", async () => {
      setStatus("ejecting…", null);
      try {
        if (isNative()) {
          await window.pywebview.api.vm_unmount(slot.device);
          await window.pywebview.api.vm_eject_url(slot.device);
        } else {
          await bridgeVmCall(
            { t: "vm_eject_url", device: slot.device },
            (m) => m.t === "vm_result" && m.device === slot.device
          );
        }
        pathEl.textContent = "no file selected";
        urlEl.value = "";
        setStatus("not connected", null);
      } catch (err) {
        setStatus(String(err), "err");
      }
    });

    ui.mediaDevices.appendChild(node);
  }
}

ui.btnMedia.addEventListener("click", () => {
  if (!ui.mediaDevices.children.length) buildMediaPanel();
  ui.mediaModal.hidden = false;
});
ui.mediaClose.addEventListener("click", () => { ui.mediaModal.hidden = true; });
ui.mediaModal.addEventListener("click", (e) => {
  if (e.target === ui.mediaModal) ui.mediaModal.hidden = true;
});
