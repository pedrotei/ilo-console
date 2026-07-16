"use strict";

// Same dual-mode pattern as ilo3console/ilo100console's own front-ends:
//   • Native desktop (pywebview): calls go straight to window.pywebview.api.
//   • Browser: calls go through the launcher's small JSON HTTP API.
function isNative() { return !!(window.pywebview && window.pywebview.api); }

const $ = (id) => document.getElementById(id);

const ui = {
  list: $("server-list"), empty: $("empty"), rowTemplate: $("server-row-template"),
  btnAdd: $("btn-add"), btnEditOrder: $("btn-edit-order"),
  formOverlay: $("form-overlay"), form: $("server-form"), formTitle: $("form-title"),
  formErr: $("form-err"), formCancel: $("form-cancel"),
  fServerId: $("server-id"), fName: $("name"), fGeneration: $("generation"),
  fHost: $("host"), fUsername: $("username"), fPort: $("port"), fSsl: $("ssl"),
  fSavePassword: $("save-password"), fPassword: $("password"),
  passwordField: $("password-field"), passwordHint: $("password-hint"),
  dangerZone: $("danger-zone"), btnDelete: $("btn-delete"),
  confirmOverlay: $("confirm-overlay"), confirmText: $("confirm-text"),
  confirmCancel: $("confirm-cancel"), confirmDelete: $("confirm-delete"),
};

let servers = [];
let deleteTargetId = null;

// -- backend calls (native vs. browser) --------------------------------------

async function apiListServers() {
  if (isNative()) return window.pywebview.api.list_servers();
  const res = await fetch("/api/servers");
  return res.json();
}

async function apiSaveServer(fields, existingId) {
  if (isNative()) {
    return existingId
      ? window.pywebview.api.update_server(existingId, fields)
      : window.pywebview.api.add_server(fields);
  }
  const res = await fetch(existingId ? `/api/servers/${existingId}` : "/api/servers", {
    method: existingId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "save failed");
  return data;
}

async function apiDeleteServer(id) {
  if (isNative()) return window.pywebview.api.remove_server(id);
  const res = await fetch(`/api/servers/${id}`, { method: "DELETE" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "delete failed");
  return data;
}

async function apiLaunch(id) {
  if (isNative()) return window.pywebview.api.launch(id);
  const res = await fetch(`/api/launch/${id}`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "launch failed");
  window.open(data.url, "_blank");
  return { ok: true };
}

async function apiReorderServers(orderedIds) {
  if (isNative()) return window.pywebview.api.reorder_servers(orderedIds);
  const res = await fetch("/api/servers/order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order: orderedIds }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "reorder failed");
  return data;
}

async function apiGetCertStatuses() {
  if (isNative()) return window.pywebview.api.get_cert_statuses();
  const res = await fetch("/api/cert-status");
  return res.json();
}

// -- rendering ----------------------------------------------------------

const GENERATION_LABEL = { ilo3: "iLO 3", ilo100: "iLO 100" };
let orderEditMode = false;
let certStatuses = {}; // server id -> latest cached status from certmonitor.py

// "No connection" -- a plain circle-slash, used when the server didn't
// respond to the check at all (nothing cert-specific to say about it).
const ICON_DISCONNECTED = `
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="9"/>
    <line x1="6" y1="6" x2="18" y2="18"/>
  </svg>`;

// A padlock; ok/warn use the plain closed shape, bad additionally gets a
// crossing line through it ("broken/insecure") -- same base glyph so the
// three cert-related states read as a family, distinct from "disconnected".
function lockIcon(broken) {
  return `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
      ${broken ? '<line x1="4" y1="4" x2="20" y2="20"/>' : ""}
    </svg>`;
}

function renderCertBadge(node, server) {
  const badge = node.querySelector(".cert-badge");
  if (server.generation !== "ilo3") return; // cert monitor only checks iLO 3
  const status = certStatuses[server.id];
  if (!status) return; // no result yet (just added, or still waiting on first check)
  const checkedAt = status.checked_at ? new Date(status.checked_at).toLocaleTimeString() : "?";
  if (!status.reachable) {
    badge.className = "cert-badge visible unknown";
    badge.innerHTML = ICON_DISCONNECTED;
    badge.title = `Server unreachable (as of ${checkedAt})\n${status.error || ""}`;
  } else if (status.invalid) {
    badge.className = "cert-badge visible bad";
    badge.innerHTML = lockIcon(true);
    const reason = status.expired ? "expired" : "not yet valid";
    badge.title =
      `Certificate ${reason} (as of ${checkedAt})\n` +
      `Subject: ${status.subject}\nIssuer: ${status.issuer}\n` +
      `Valid: ${status.not_before} to ${status.not_after}`;
  } else if (status.days_remaining <= 14) {
    badge.className = "cert-badge visible warn";
    badge.innerHTML = lockIcon(false);
    badge.title =
      `Certificate expires in ${status.days_remaining} day(s) (as of ${checkedAt})\n` +
      `Subject: ${status.subject}\nIssuer: ${status.issuer}\nExpires: ${status.not_after}`;
  } else {
    badge.className = "cert-badge visible ok";
    badge.innerHTML = lockIcon(false);
    badge.title =
      `Certificate OK -- ${status.days_remaining} day(s) left (as of ${checkedAt})\n` +
      `Subject: ${status.subject}\nIssuer: ${status.issuer}${status.self_signed ? " (self-signed)" : ""}\n` +
      `Expires: ${status.not_after}`;
  }
}

function renderServers() {
  ui.list.innerHTML = "";
  ui.empty.hidden = servers.length > 0;
  ui.list.classList.toggle("reordering", orderEditMode);
  servers.forEach((server, i) => {
    const node = ui.rowTemplate.content.cloneNode(true);
    const row = node.querySelector(".server-row");
    const handle = node.querySelector(".drag-handle");
    row.dataset.index = String(i);
    handle.draggable = orderEditMode;
    node.querySelector(".badge").textContent = GENERATION_LABEL[server.generation] || server.generation;
    node.querySelector(".name-text").textContent = server.name;
    const detailParts = [server.host];
    if (server.username) detailParts.push(server.username);
    if (server.port) detailParts.push(`port ${server.port}`);
    if (server.ssl) detailParts.push("ssl");
    if (server.has_saved_password) detailParts.push("🔒 password saved");
    node.querySelector(".server-detail").textContent = detailParts.join(" · ");
    renderCertBadge(node, server);
    node.querySelector(".btn-edit").addEventListener("click", (e) => {
      e.stopPropagation(); // don't also trigger the row's click-to-connect below
      openForm(server);
    });
    row.addEventListener("click", () => {
      if (orderEditMode) return; // reordering: clicking the box shouldn't launch it
      connect(server.id);
    });
    wireDragHandlers(row, handle);
    ui.list.appendChild(node);
  });
}

ui.btnEditOrder.addEventListener("click", () => {
  orderEditMode = !orderEditMode;
  ui.btnEditOrder.textContent = orderEditMode ? "Done" : "Edit Order";
  ui.btnEditOrder.classList.toggle("active", orderEditMode);
  renderServers();
});

// -- drag-and-drop reorder (only active while orderEditMode is on) --------

let dragSourceIndex = null;

function wireDragHandlers(row, handle) {
  // The handle -- not the row -- is the draggable element. An <svg> icon
  // inside a draggable row has its own implicit native drag behavior (like
  // an <img>) that can intercept the gesture before a row-level dragstart
  // ever fires; scoping `draggable` to the handle itself (with the icon
  // set pointer-events: none, see CSS) avoids that entirely.
  handle.addEventListener("dragstart", (e) => {
    dragSourceIndex = Number(row.dataset.index);
    e.dataTransfer.effectAllowed = "move";
    // Firefox requires data to actually be set for the drag to proceed.
    e.dataTransfer.setData("text/plain", row.dataset.index);
    // Only the (small) handle is draggable, so the browser's default drag
    // image would just be that icon -- use the whole row instead, anchored
    // under the cursor at the point it was actually grabbed. Must happen
    // before the opacity change below, or the captured image is the faded one.
    const rowRect = row.getBoundingClientRect();
    e.dataTransfer.setDragImage(row, e.clientX - rowRect.left, e.clientY - rowRect.top);
    row.classList.add("dragging");
  });
  handle.addEventListener("dragend", () => {
    row.classList.remove("dragging");
    for (const el of ui.list.querySelectorAll(".drag-over")) el.classList.remove("drag-over");
    dragSourceIndex = null;
  });
  row.addEventListener("dragenter", (e) => {
    if (dragSourceIndex === null) return;
    e.preventDefault();
  });
  row.addEventListener("dragover", (e) => {
    if (dragSourceIndex === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    row.classList.add("drag-over");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("drag-over");
    const targetIndex = Number(row.dataset.index);
    if (dragSourceIndex === null || targetIndex === dragSourceIndex) return;
    moveServer(dragSourceIndex, targetIndex);
  });
}

async function moveServer(fromIndex, toIndex) {
  const reordered = servers.slice();
  const [moved] = reordered.splice(fromIndex, 1);
  reordered.splice(toIndex, 0, moved);
  const previous = servers;
  servers = reordered; // optimistic: re-render immediately, revert on failure
  renderServers();
  try {
    await apiReorderServers(reordered.map((s) => s.id));
  } catch (err) {
    servers = previous;
    renderServers();
    alert(`Couldn't reorder servers: ${err.message || err}`);
  }
}

// Boot calls this both eagerly and again on "pywebviewready" (see below) --
// window.pywebview.api isn't populated yet at the moment this script first
// runs, so isNative() can't be trusted for that very first call and may
// wrongly take the fetch() path in native mode. Whichever call *started*
// last wins, regardless of which happens to *finish* last.
let loadGeneration = 0;

async function loadServers() {
  const gen = ++loadGeneration;
  let result;
  try {
    result = await apiListServers();
  } catch {
    return; // likely the too-early native call hitting a nonexistent fetch
             // target; the pywebviewready-triggered retry will succeed.
  }
  if (gen !== loadGeneration) return; // a newer load has already superseded this one
  servers = result;
  renderServers();
}

// -- connect --------------------------------------------------------------

async function connect(id) {
  try {
    const result = await apiLaunch(id);
    if (result && result.ok === false) throw new Error(result.error || "launch failed");
  } catch (err) {
    alert(`Couldn't launch console: ${err.message || err}`);
  }
}

// -- add/edit form --------------------------------------------------------

function updatePasswordFieldVisibility(hasSavedPassword) {
  ui.passwordField.hidden = !ui.fSavePassword.checked;
  ui.passwordHint.textContent = hasSavedPassword
    ? "A password is already saved. Leave blank to keep it, or type a new one to replace it."
    : "";
}

ui.fSavePassword.addEventListener("change", () => {
  updatePasswordFieldVisibility(ui.fSavePassword.dataset.hasSaved === "1");
});

let editingServer = null;

function openForm(server) {
  ui.formErr.textContent = "";
  ui.form.reset();
  editingServer = server;
  ui.dangerZone.hidden = !server;
  if (server) {
    ui.formTitle.textContent = "Edit server";
    ui.fServerId.value = server.id;
    ui.fName.value = server.name;
    ui.fGeneration.value = server.generation;
    ui.fHost.value = server.host;
    ui.fUsername.value = server.username || "";
    ui.fPort.value = server.port || "";
    ui.fSsl.checked = !!server.ssl;
    ui.fSavePassword.checked = !!server.has_saved_password;
    ui.fSavePassword.dataset.hasSaved = server.has_saved_password ? "1" : "0";
  } else {
    ui.formTitle.textContent = "Add server";
    ui.fServerId.value = "";
    ui.fSavePassword.dataset.hasSaved = "0";
  }
  updatePasswordFieldVisibility(ui.fSavePassword.dataset.hasSaved === "1");
  ui.formOverlay.hidden = false;
  ui.fName.focus();
}

function closeForm() { ui.formOverlay.hidden = true; }

ui.btnAdd.addEventListener("click", () => openForm(null));
ui.formCancel.addEventListener("click", closeForm);
ui.formOverlay.addEventListener("click", (e) => { if (e.target === ui.formOverlay) closeForm(); });

ui.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  ui.formErr.textContent = "";
  const fields = {
    name: ui.fName.value.trim(),
    generation: ui.fGeneration.value,
    host: ui.fHost.value.trim(),
    username: ui.fUsername.value.trim(),
    port: ui.fPort.value.trim() || null,
    ssl: ui.fSsl.checked,
    save_password: ui.fSavePassword.checked,
    password: ui.fPassword.value,
  };
  try {
    await apiSaveServer(fields, ui.fServerId.value || null);
    closeForm();
    await loadServers();
  } catch (err) {
    ui.formErr.textContent = err.message || String(err);
  }
});

// -- delete confirm (triggered from the edit dialog's danger zone) --------

function openConfirm(server) {
  deleteTargetId = server.id;
  ui.confirmText.textContent = `Delete "${server.name}"? This can't be undone.`;
  ui.confirmOverlay.hidden = false;
}
function closeConfirm() { ui.confirmOverlay.hidden = true; deleteTargetId = null; }

ui.btnDelete.addEventListener("click", () => {
  if (editingServer) openConfirm(editingServer);
});
ui.confirmCancel.addEventListener("click", closeConfirm);
ui.confirmOverlay.addEventListener("click", (e) => { if (e.target === ui.confirmOverlay) closeConfirm(); });
ui.confirmDelete.addEventListener("click", async () => {
  if (!deleteTargetId) return;
  try {
    await apiDeleteServer(deleteTargetId);
    closeConfirm();
    closeForm();
    await loadServers();
  } catch (err) {
    alert(`Couldn't delete server: ${err.message || err}`);
    closeConfirm();
  }
});

// -- cert status polling ---------------------------------------------------
// certmonitor.py re-checks every 60s server-side and caches the result;
// this just reads that cache, so polling here is cheap (no live network
// call per poll). Skips the DOM refresh (but keeps the fetched data) while
// a drag is in flight -- rebuilding the row list mid-drag would cancel the
// browser's native drag operation out from under the user.
async function refreshCertStatuses() {
  try {
    certStatuses = await apiGetCertStatuses();
  } catch {
    return; // same reasoning as loadServers(): too-early native call, ignore
  }
  if (dragSourceIndex === null) renderServers();
}

function allIlo3ServersHaveCertStatus() {
  return servers.every((s) => s.generation !== "ilo3" || certStatuses[s.id]);
}

// The backend runs its own first check at launch (see certmonitor.start()),
// but each server can take up to several seconds (connect timeout) to
// resolve, and this page's own first poll can easily land before that
// finishes -- especially since `servers` itself is still loading around the
// same time. Rather than one eager poll and then a 60s wait for the *next*
// cycle, retry every couple of seconds until every known iLO 3 server has a
// result (or we give up after a while and just fall back to the normal
// interval below).
async function pollCertStatusesUntilFresh() {
  for (let attempt = 0; attempt < 20; attempt++) {
    await refreshCertStatuses();
    if (allIlo3ServersHaveCertStatus()) return;
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

// -- boot ---------------------------------------------------------------
// Always both: an eager call (correct immediately in a real browser, where
// window.pywebview will just never exist) and a pywebviewready-triggered
// retry (correct in native mode, where the API isn't wired up yet at this
// point). loadServers()'s generation guard makes whichever one actually
// applies win, regardless of exact timing.

window.addEventListener("pywebviewready", loadServers, { once: true });
window.addEventListener("pywebviewready", pollCertStatusesUntilFresh, { once: true });
loadServers();
pollCertStatusesUntilFresh();
setInterval(refreshCertStatuses, 60000);
