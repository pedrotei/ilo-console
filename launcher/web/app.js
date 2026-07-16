"use strict";

// Same dual-mode pattern as ilo3console/ilo100console's own front-ends:
//   • Native desktop (pywebview): calls go straight to window.pywebview.api.
//   • Browser: calls go through the launcher's small JSON HTTP API.
function isNative() { return !!(window.pywebview && window.pywebview.api); }

const $ = (id) => document.getElementById(id);

const ui = {
  list: $("server-list"), empty: $("empty"), rowTemplate: $("server-row-template"),
  btnAdd: $("btn-add"),
  formOverlay: $("form-overlay"), form: $("server-form"), formTitle: $("form-title"),
  formErr: $("form-err"), formCancel: $("form-cancel"),
  fServerId: $("server-id"), fName: $("name"), fGeneration: $("generation"),
  fHost: $("host"), fUsername: $("username"), fPort: $("port"), fSsl: $("ssl"),
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

// -- rendering ----------------------------------------------------------

const GENERATION_LABEL = { ilo3: "iLO 3", ilo100: "iLO 100" };

function renderServers() {
  ui.list.innerHTML = "";
  ui.empty.hidden = servers.length > 0;
  for (const server of servers) {
    const node = ui.rowTemplate.content.cloneNode(true);
    node.querySelector(".badge").textContent = GENERATION_LABEL[server.generation] || server.generation;
    node.querySelector(".name-text").textContent = server.name;
    const detailParts = [server.host];
    if (server.username) detailParts.push(server.username);
    if (server.port) detailParts.push(`port ${server.port}`);
    if (server.ssl) detailParts.push("ssl");
    node.querySelector(".server-detail").textContent = detailParts.join(" · ");
    node.querySelector(".btn-connect").addEventListener("click", () => connect(server.id));
    node.querySelector(".btn-edit").addEventListener("click", () => openForm(server));
    node.querySelector(".btn-delete").addEventListener("click", () => openConfirm(server));
    ui.list.appendChild(node);
  }
}

async function loadServers() {
  servers = await apiListServers();
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

function openForm(server) {
  ui.formErr.textContent = "";
  ui.form.reset();
  if (server) {
    ui.formTitle.textContent = "Edit server";
    ui.fServerId.value = server.id;
    ui.fName.value = server.name;
    ui.fGeneration.value = server.generation;
    ui.fHost.value = server.host;
    ui.fUsername.value = server.username || "";
    ui.fPort.value = server.port || "";
    ui.fSsl.checked = !!server.ssl;
  } else {
    ui.formTitle.textContent = "Add server";
    ui.fServerId.value = "";
  }
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
  };
  try {
    await apiSaveServer(fields, ui.fServerId.value || null);
    closeForm();
    await loadServers();
  } catch (err) {
    ui.formErr.textContent = err.message || String(err);
  }
});

// -- delete confirm ---------------------------------------------------------

function openConfirm(server) {
  deleteTargetId = server.id;
  ui.confirmText.textContent = `Delete "${server.name}"? This can't be undone.`;
  ui.confirmOverlay.hidden = false;
}
function closeConfirm() { ui.confirmOverlay.hidden = true; deleteTargetId = null; }

ui.confirmCancel.addEventListener("click", closeConfirm);
ui.confirmOverlay.addEventListener("click", (e) => { if (e.target === ui.confirmOverlay) closeConfirm(); });
ui.confirmDelete.addEventListener("click", async () => {
  if (!deleteTargetId) return;
  try {
    await apiDeleteServer(deleteTargetId);
    closeConfirm();
    await loadServers();
  } catch (err) {
    alert(`Couldn't delete server: ${err.message || err}`);
    closeConfirm();
  }
});

// -- boot ---------------------------------------------------------------

if (isNative()) {
  window.addEventListener("pywebviewready", loadServers, { once: true });
} else {
  loadServers();
}
