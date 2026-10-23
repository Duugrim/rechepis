import { api } from "./api.js";
import { connectEvents } from "./events.js";
import { offerModel } from "./firstrun.js";
import "./inspector.js";
import { refreshLibrary } from "./library.js";
import { emit, on, state } from "./state.js";
import "./settings.js";
import { $, PROVIDERS, PROVIDER_IDS, esc, guard, hideOverlay, setStatus, showOverlay, store } from "./util.js";
import { initWorkspace, openRecording, reloadRecording, renderRecorder } from "./workspace.js";

let connected = true;

function applyBootstrap(data) {
  state.config = data.config;
  state.providers = data.providers;
  state.queues = data.queues || {};
  state.presets = data.presets;
  state.gigaModels = data.gigaam_models;
}

function renderPills() {
  const pills = PROVIDER_IDS.map((id) => {
    const p = state.providers[id];
    if (!p) return "";
    const dl = p.download;
    let cls = p.ready ? "ok" : "warn";
    let text = p.ready ? "готов" : p.state === "not_installed" ? "не установлен" : p.state === "no_model" ? "нет модели" : p.state;
    if (p.state === "downloading") {
      cls = "info";
      text = dl?.total ? `скачивание ${Math.round((dl.done / dl.total) * 100)}%` : "скачивание…";
    } else if (p.state === "loading") {
      cls = "info";
      text = "загрузка…";
    }
    const q = state.queues[id];
    const busy = q && (q.queued || q.running) ? ` · ${q.running + q.queued}` : "";
    return `<button type="button" class="pill" data-open="providers" title="${esc(p.message || "")}"><span class="dot ${cls === "info" ? "busy" : cls}"></span>${PROVIDERS[id].name} · ${esc(text)}${busy}</button>`;
  });
  if (!connected) pills.unshift(`<span class="pill" style="border-color:var(--danger);color:#f3b3a6"><span class="dot bad"></span>нет связи с программой</span>`);
  $("providerPills").innerHTML = pills.join("");
}

$("providerPills").addEventListener("click", (event) => {
  if (event.target.closest("[data-open]")) emit("open-settings", "providers");
});
$("settingsBtn").addEventListener("click", () => emit("open-settings", "providers"));

on("providers", () => {
  renderPills();
});
on("connection", (ok) => {
  connected = ok;
  renderPills();
});
on("open-recording", (id) => guard(() => openRecording(id)));
on("reconnected", () => guard(async () => {
  const cfg = await api("/config");
  applyBootstrap(cfg);
  renderPills();
  await refreshLibrary();
  await reloadRecording();
}));

async function init() {
  showOverlay("Запуск…");
  try {
    applyBootstrap(await api("/config"));
    renderPills();
    connectEvents();
    await initWorkspace();
    await refreshLibrary();
    const last = store("rec");
    if (last && state.recordings.some((r) => r.id === last)) await openRecording(last);
    else if (state.recordings[0]) await openRecording(state.recordings[0].id);
    renderRecorder();
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    hideOverlay();
  }
  offerModel(); // the model is downloaded once; on the very first start this is the first thing the user sees
}

init();
