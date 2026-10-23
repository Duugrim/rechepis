import { api, enc } from "./api.js";
import { $, debounce, esc, fmtClock, fmtDate, guard, setStatus } from "./util.js";
import { emit, on, state } from "./state.js";

const els = {
  list: $("libList"),
  count: $("libCount"),
  filter: $("libFilter"),
  form: $("newForm"),
  title: $("newTitle"),
};

export async function refreshLibrary() {
  const data = await api("/recordings");
  state.recordings = data.recordings;
  renderLibrary();
}

export function renderLibrary() {
  const q = els.filter.value.trim().toLowerCase();
  const items = state.recordings.filter(
    (r) => !q || r.title.toLowerCase().includes(q) || (r.transcript_preview || "").toLowerCase().includes(q)
  );
  els.count.textContent = state.recordings.length ? `${state.recordings.length}` : "";
  if (!items.length) {
    els.list.innerHTML = `<p class="muted" style="padding:.4rem .2rem;font-size:.85rem">${
      state.recordings.length ? "Ничего не найдено." : "Пока нет записей. Введите тему и нажмите «+»."
    }</p>`;
    return;
  }
  els.list.innerHTML = items
    .map((r) => {
      const dur = r.duration_seconds ? fmtClock(r.duration_seconds) : "";
      const badges = [
        r.working ? `<span class="badge info"><span class="spinner" style="width:.55rem;height:.55rem"></span>${r.working}</span>` : "",
        r.errors ? `<span class="badge bad" title="Сегментов с ошибкой">⚠ ${r.errors}</span>` : "",
      ].join("");
      return `<div class="lib-item ${r.id === state.recId ? "active" : ""}" data-id="${esc(r.id)}">
        <div class="lib-title">${esc(r.title)}</div>
        <div class="lib-meta"><span>${esc(fmtDate(r.created_at))}</span>${
          r.session_count ? `<span>${r.session_count} сесс.</span><span>${r.segment_count} сегм.</span>` : ""
        }${dur ? `<span>${dur}</span>` : ""}${badges}</div>
        ${r.transcript_preview ? `<div class="lib-preview">${esc(r.transcript_preview)}</div>` : ""}
      </div>`;
    })
    .join("");
}

const refreshSoon = debounce(() => guard(refreshLibrary), 800);
on("library-dirty", refreshSoon);

els.filter.addEventListener("input", renderLibrary);
els.list.addEventListener("click", (event) => {
  const item = event.target.closest(".lib-item");
  if (item) emit("open-recording", item.dataset.id);
});

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const title = els.title.value.trim();
  if (!title) {
    els.title.focus();
    return;
  }
  guard(async () => {
    const created = await api("/recordings", { method: "POST", body: { title } });
    els.title.value = "";
    await refreshLibrary();
    emit("open-recording", created.id);
    setStatus("Запись создана. Нажмите «Начать сессию», когда будете готовы говорить.", "ok");
  });
});

export const renameRecording = (id, title) => api(`/recordings/${enc(id)}`, { method: "PATCH", body: { title } });
