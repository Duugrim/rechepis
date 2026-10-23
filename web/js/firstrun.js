import { api } from "./api.js";
import { emit, modelMissing, on, state } from "./state.js";
import { $, esc, guard, setStatus } from "./util.js";

/**
 * Model setup. The recognition model is not part of the program: it is downloaded once. This dialog offers it on
 * the first start and whenever the user reaches for a model that is missing, shows the progress, and loads the
 * model into memory when it is done so that the first segment does not wait for it.
 * The download itself is the server's (resumable, checksummed); this is only its face.
 */

const root = $("modelSetup");
let open = false;
let dismissed = false; // "Позже" holds until the page is reloaded
let lastState = null;

const gigaam = () => state.providers.gigaam;
const chosenModel = () => state.config?.gigaam?.model;
const sizeMb = () => state.gigaModels?.[chosenModel()]?.size_mb;
const mb = (bytes) => Math.round(bytes / 1048576);

function card(title, body, actions) {
  return `<div class="modal-card" style="max-width:32rem">
    <div class="panel-head"><h2>${title}</h2></div>
    ${body}
    <div class="row" style="justify-content:flex-end">${actions}</div>
  </div>`;
}

const button = (act, label, kind = "secondary") => `<button type="button" class="btn ${kind}" data-act="${act}">${label}</button>`;

function viewHtml() {
  const g = gigaam();
  const dl = g.download || {};
  if (g.downloaded) {
    return card(
      "Модель готова",
      `<p class="explain">Можно записывать. Расшифровка идёт на вашем компьютере, интернет больше не нужен.</p>`,
      button("done", "Начать работу", "primary"),
    );
  }
  if (dl.state === "downloading") {
    const pct = dl.total ? Math.round((dl.done / dl.total) * 100) : 0;
    const progress = dl.total ? `${pct}% · ${mb(dl.done)} из ${mb(dl.total)} МБ` : "подключаюсь…";
    return card(
      "Скачиваю модель…",
      `<div class="progress"><span style="width:${pct}%"></span></div>
       <p class="explain mono" style="margin:0">${progress}</p>
       <p class="explain">Окно можно скрыть: скачивание продолжится, ход виден вверху рядом с названием модели. Если связь оборвётся, программа докачает файл сама.</p>`,
      button("hide", "Скрыть"),
    );
  }
  if (dl.state === "error") {
    return card(
      "Не удалось скачать модель",
      `<div class="notice bad">${esc(dl.error || "неизвестная ошибка")}</div>
       <p class="explain">Нажмите «Повторить». Если дело было в связи, скачивание продолжится с того места, где оборвалось.</p>`,
      `${button("hide", "Закрыть", "ghost")}${button("download", "Повторить", "primary")}`,
    );
  }
  const size = sizeMb() ? `около ${sizeMb()} МБ` : "несколько сотен мегабайт";
  return card(
    "Нужна модель распознавания",
    `<p class="explain">Речепись превращает речь в текст моделью <b>GigaAM</b> прямо на вашем компьютере: голос никуда не уходит.
       Модель нужно скачать один раз (${size}, с серверов Сбера). После этого интернет не нужен.</p>
     <p class="explain" style="margin:0">Пока модели нет, записывать и резать звук можно, выбрав «Не отправлять», — расшифровать записанное можно позже.
       <span class="link" data-act="settings">Выбрать другую модель</span></p>`,
    `${button("later", "Позже", "ghost")}${button("download", `Скачать модель${sizeMb() ? ` · ${sizeMb()} МБ` : ""}`, "primary")}`,
  );
}

function render() {
  if (!open) return;
  root.innerHTML = viewHtml();
  root.classList.remove("hidden");
}

function close() {
  open = false;
  root.classList.add("hidden");
  root.innerHTML = "";
}

/** Show the dialog. Without the packages there is nothing to download: the settings explain what to install. */
export function openSetup() {
  if (!gigaam()?.installed) return emit("open-settings", "providers");
  open = true;
  render();
}

/** Called once the program has loaded: offer the model if it is missing (unless the user said "later"). */
export function offerModel() {
  if (modelMissing() && !dismissed) openSetup();
}

async function warmUp() {
  try {
    await api("/providers/gigaam/warm", { method: "POST" });
  } catch {
    /* it loads on the first segment anyway */
  }
}

root.addEventListener("click", (event) => {
  if (event.target === root) {
    dismissed = true; // a click on the backdrop is the same as "Позже"
    return close();
  }
  const el = event.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;
  if (act === "later") {
    dismissed = true;
    return close();
  }
  if (act === "hide") return close();
  if (act === "settings") {
    close();
    return emit("open-settings", "providers");
  }
  if (act === "done") {
    close();
    return void warmUp();
  }
  if (act === "download") {
    return void guard(async () => {
      const res = await api("/providers/gigaam/download", { method: "POST", body: { model: chosenModel() } });
      state.providers.gigaam = res.provider;
      emit("providers");
    });
  }
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && open) {
    dismissed = true;
    close();
  }
});

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-open-model]")) openSetup();
});

on("open-model-setup", openSetup);

on("providers", () => {
  const g = gigaam();
  const dl = g?.download?.state;
  const now = dl === "downloading" ? "downloading" : g?.downloaded ? "ready" : dl === "error" ? "failed" : "other";
  const wasDownloading = lastState === "downloading";
  lastState = now;
  if (wasDownloading && now === "ready") {
    void warmUp();
    if (!open) setStatus("Модель скачана — можно расшифровывать", "ok", null, 5000);
  } else if (wasDownloading && now === "failed" && !open) {
    // the dialog was hidden while it downloaded: do not let a failure pass unnoticed
    setStatus("Не удалось скачать модель", "error", { label: "Подробнее", onClick: openSetup });
  }
  render();
});
