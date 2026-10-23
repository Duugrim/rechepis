import { api, enc } from "./api.js";
import { renderRecorder } from "./workspace.js";
import { emit, on, session, state } from "./state.js";
import { $, TARGET_CHOICES, debounce, esc, fmtDur, guard, setStatus } from "./util.js";
import { drawWave } from "./waveform.js";
import { listMics } from "./recorder.js";

/* =============================== parameter definitions =============================== */

const SLICING_FIELDS = [
  { key: "min_len_s", label: "Минимальная длина сегмента", unit: "с", min: 0.5, max: 15, step: 0.5,
    hint: "Короче этого сегмент не делается — кроме случая, когда после него долгая пауза." },
  { key: "target_len_s", label: "Желаемая длина", unit: "с", min: 2, max: 60, step: 1,
    hint: "Набрав столько речи, программа режет на первой подходящей паузе. Чем короче — тем меньше контекста у модели." },
  { key: "max_len_s", label: "Максимальная длина", unit: "с", min: 5, max: 120, step: 1,
    hint: "Жёсткий предел. GigaAM принимает до 25 с за раз; более длинный сегмент модель режет внутри себя по паузам." },
  { key: "pause_ms", label: "Пауза для разреза", unit: "мс", min: 100, max: 2000, step: 50,
    hint: "Какой провал в речи считается паузой, когда сегмент уже достаточно длинный." },
  { key: "hard_pause_ms", label: "Долгая пауза (конец мысли)", unit: "мс", min: 500, max: 6000, step: 100,
    hint: "Такая пауза завершает сегмент всегда. Тишина длиннее выбрасывается — модели не подсовывается молчание." },
  { key: "pad_ms", label: "Запас по краям сегмента", unit: "мс", min: 0, max: 800, step: 50,
    hint: "Сколько тишины оставить до и после речи, чтобы не обрезать слова." },
  { key: "sensitivity_db", label: "Чувствительность к речи", unit: "дБ над шумом", min: 3, max: 25, step: 1,
    hint: "Меньше — ловит тихую речь, но может принять шум за речь. Больше — надёжнее в шумной комнате." },
  { key: "pause_depth_db", label: "Глубина паузы", unit: "дБ ниже речи", min: 6, max: 40, step: 1,
    hint: "Насколько тише обычной речи должен быть провал. Если сегменты не режутся (пауз «нет») — уменьшите; если режет посреди фраз — увеличьте." },
];

const PROCESSING_FIELDS = [
  { key: "highpass_hz", label: "Срез низких частот", unit: "Гц", min: 0, max: 300, step: 10,
    hint: "Убирает гул, стук по столу и дыхание в микрофон. 0 — выключено." },
  { key: "denoise", label: "Подавление шума", unit: "%", min: 0, max: 100, step: 5, scale: 100,
    hint: "Программа изучает шум комнаты по паузам и вычитает его (фильтр Винера). Убирает ровный шум: вентилятор, шипение, гул сети." },
  { key: "normalize", label: "Выровнять громкость", type: "check",
    hint: "Привести громкость речи в каждом сегменте к целевой, пики аккуратно ограничить." },
  { key: "target_dbfs", label: "Целевая громкость речи", unit: "дБFS", min: -35, max: -10, step: 1,
    hint: "Средний уровень речи после усиления. −20 — обычный разговорный." },
  { key: "max_gain_db", label: "Предел усиления", unit: "дБ", min: 0, max: 40, step: 1,
    hint: "Тихие записи не раздуваются сверх этого — иначе вместе с речью растёт шум." },
];

const fieldValue = (def, v) => (def.type === "check" ? "" : `${def.scale ? Math.round(v * def.scale) : v} ${def.unit}`);

function fieldsHtml(defs, values, ns) {
  return defs
    .map((d) => {
      const v = values[d.key];
      if (d.type === "check") {
        return `<div class="field"><label class="check" style="color:var(--ink)"><input type="checkbox" data-ns="${ns}" data-key="${d.key}" ${v ? "checked" : ""}> ${esc(d.label)}</label><div class="sub">${esc(d.hint)}</div></div>`;
      }
      const shown = d.scale ? Math.round(v * d.scale) : v;
      return `<div class="field"><label class="lbl"><span>${esc(d.label)}</span><span class="val" data-val="${ns}.${d.key}">${esc(fieldValue(d, v))}</span></label>
        <input type="range" data-ns="${ns}" data-key="${d.key}" min="${d.min}" max="${d.max}" step="${d.step}" value="${shown}">
        <div class="sub">${esc(d.hint)}</div></div>`;
    })
    .join("");
}

function readField(input, defs) {
  const def = defs.find((d) => d.key === input.dataset.key);
  if (def.type === "check") return input.checked;
  const n = Number(input.value);
  return def.scale ? n / def.scale : n;
}

const samePreset = (a, b) => Object.keys(a).every((k) => a[k] === b[k]);
function matchingPreset(presets, params) {
  return Object.keys(presets).find((name) => samePreset(presets[name].params, params)) || "custom";
}

/* =============================== drawer =============================== */

const drawer = $("drawer");
const scrim = $("scrim");
let tab = "providers";
let flashTimer = null;

const TABS = [
  ["providers", "Распознавание"],
  ["slicing", "Нарезка"],
  ["processing", "Звук"],
  ["mic", "Микрофон"],
  ["general", "Общие"],
];

export function openSettings(which) {
  if (which) tab = which === "providers" || TABS.some(([id]) => id === which) ? which : "providers";
  drawer.classList.remove("hidden");
  scrim.classList.remove("hidden");
  renderDrawer();
}

export function closeSettings() {
  drawer.classList.add("hidden");
  scrim.classList.add("hidden");
}

function flashSaved() {
  const note = drawer.querySelector(".saved-note");
  if (!note) return;
  note.classList.add("show");
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => note.classList.remove("show"), 1400);
}

async function save(patch) {
  const res = await api("/config", { method: "PUT", body: patch });
  state.config = res.config;
  state.providers = res.providers;
  state.presets = res.presets;
  flashSaved();
  renderRecorder();
  emit("providers");
  return res;
}

const saveSoon = debounce((patch) => guard(() => save(patch)), 450);

function renderDrawer() {
  if (drawer.classList.contains("hidden")) return;
  const body = drawer.querySelector(".drawer-body");
  const scroll = body ? body.scrollTop : 0;
  drawer.innerHTML = `<div class="drawer-head"><h2>Настройки</h2><span class="saved-note">сохранено</span><button type="button" class="btn ghost" data-act="close">✕</button></div>
    <div class="tabs">${TABS.map(([id, label]) => `<button type="button" data-tab="${id}" class="${id === tab ? "on" : ""}">${label}</button>`).join("")}</div>
    <div class="drawer-body">${{ providers: providersTab, slicing: slicingTab, processing: processingTab, mic: micTab, general: generalTab }[tab]()}</div>`;
  drawer.querySelector(".drawer-body").scrollTop = scroll;
}

/* ---------- providers ---------- */

function statusPill(p) {
  if (!p) return "";
  const cls = p.state === "ready" ? "ok" : p.ready ? "info" : p.state === "downloading" ? "info" : "warn";
  return `<span class="badge ${cls}">${esc(p.message || p.state)}</span>`;
}

function providersTab() {
  const cfg = state.config;
  const giga = state.providers.gigaam;
  const dl = giga?.download || {};
  const models = Object.entries(state.gigaModels || {})
    .map(([id, m]) => {
      const info = giga?.models?.[id] || {};
      const picked = cfg.gigaam.model === id;
      const downloading = dl.state === "downloading" && dl.model === id;
      const pct = dl.total ? Math.round((dl.done / dl.total) * 100) : 0;
      const action = downloading
        ? `<span class="mono muted" style="font-size:.74rem">${pct}% · ${(dl.done / 1048576).toFixed(0)}/${(dl.total / 1048576).toFixed(0)} МБ</span>`
        : info.downloaded
          ? `<span class="badge ok">скачана</span>`
          : `<button type="button" class="btn secondary sm" data-act="download" data-model="${id}" ${giga?.installed ? "" : "disabled"}>Скачать · ${m.size_mb} МБ</button>`;
      return `<div class="model-row ${picked ? "picked" : ""}">
        <input type="radio" name="gmodel" data-act="pick-model" value="${id}" ${picked ? "checked" : ""}>
        <div><div class="nm">${esc(m.label)} ${m.punctuation ? `<span class="badge accent">пунктуация</span>` : ""}</div><div class="nt">${esc(m.note)}</div>
          ${downloading ? `<div class="bar"><span style="width:${pct}%"></span></div>` : ""}${dl.state === "error" && dl.model === id ? `<div class="nt" style="color:var(--danger)">Ошибка скачивания: ${esc(dl.error)}</div>` : ""}</div>
        <div>${action}</div></div>`;
    })
    .join("");
  const install = giga && !giga.installed
    ? `<div class="notice">Пакеты GigaAM (PyTorch и др., ≈ 1 ГБ) не установлены. Запустите <b>Install GigaAM.bat</b> в папке программы, затем перезапустите Речепись. Или вручную:
        <pre class="cmd" style="margin-top:.4rem">.venv\\Scripts\\python.exe -m pip install -r requirements-gigaam.txt</pre></div>` : "";
  const t = cfg.targets;
  return `
  <div class="card"><div class="card-head"><h3>По умолчанию</h3></div>
    <div class="field"><label class="lbl"><span>Куда отправлять новые сегменты</span></label>
      <div class="seg-ctl" id="sTargets">
        ${TARGET_CHOICES
          .map(({ label, list }) => `<button type="button" data-act="targets" data-t='${JSON.stringify(list)}' class="${list.length === t.length && list.every((x) => t.includes(x)) ? "on" : ""}">${label}</button>`)
          .join("")}</div>
      <div class="sub">Это значение подставляется на панели записи; для каждой сессии его можно изменить.</div></div>
  </div>

  <div class="card"><div class="card-head"><h3>GigaAM v3 · локально</h3>${statusPill(giga)}</div>
    <div class="explain">Открытая модель Сбера (MIT) работает на этом компьютере, без интернета и без отправки голоса куда-либо. Принимает до 25 секунд за раз — поэтому нарезка на сегменты ей особенно подходит.</div>
    ${install}
    <div style="display:flex;flex-direction:column;gap:.4rem">${models}</div>
    <div class="fields">
      <div class="field"><label class="lbl"><span>Устройство</span></label>
        <select data-act="giga-device"><option value="auto" ${cfg.gigaam.device === "auto" ? "selected" : ""}>авто</option><option value="cpu" ${cfg.gigaam.device === "cpu" ? "selected" : ""}>процессор</option><option value="cuda" ${cfg.gigaam.device === "cuda" ? "selected" : ""}>видеокарта (CUDA)</option></select>
        <div class="sub">Сейчас: ${esc(giga?.device || "модель не загружена")}.</div></div>
      <div class="field"><label class="lbl"><span>Потоки процессора</span></label>
        <input type="number" data-act="giga-threads" min="0" max="64" value="${cfg.gigaam.threads}"><div class="sub">0 — все ядра. Меньше — компьютер отзывчивее во время записи.</div></div>
    </div>
    <div class="row"><button type="button" class="btn secondary sm" data-act="warm" ${giga?.ready ? "" : "disabled"}>Загрузить в память сейчас</button>
      <span class="muted" style="font-size:.78rem">Иначе модель загрузится при первом сегменте (≈ 4 с).</span></div>
  </div>`;
}

/* ---------- slicing and processing ---------- */

function presetChips(kind) {
  const presets = state.presets[kind];
  const current = state.config[`${kind}_preset`];
  return `<div class="presets">${Object.entries(presets)
    .map(([id, p]) => `<button type="button" class="chip ${id === current ? "on" : ""}" data-act="preset" data-kind="${kind}" data-preset="${id}">${esc(p.label)}</button>`)
    .join("")}${current === "custom" ? `<span class="chip on">Свои настройки</span>` : ""}</div>`;
}

function slicingTab() {
  const s = state.config.slicing;
  return `<div class="card"><div class="card-head"><h3>Как режется сессия</h3></div>
    <div class="explain">Пока вы говорите, программа следит за громкостью. Как только набралась <b>желаемая длина</b> и случилась <b>пауза</b> — сегмент закрывается и сразу уходит на расшифровку. Долгая тишина между фразами выбрасывается. Это значения для <b>новых</b> сессий; готовую сессию можно пересобрать с другими значениями кнопкой «Пересобрать…».</div>
    ${presetChips("slicing")}</div>
    <div class="card"><div class="fields">${fieldsHtml(SLICING_FIELDS, s, "slicing")}</div></div>`;
}

function processingTab() {
  const p = state.config.processing;
  return `<div class="card"><div class="card-head"><h3>Что делается со звуком перед отправкой</h3></div>
    <div class="explain">Оригинал записи не меняется никогда — обрабатывается копия каждого сегмента, причём заново, если вы отправляете его ещё раз с другими настройками. Порядок: срез низких → шумоподавление → выравнивание громкости → ограничитель пиков.
      <br><br><b>Честно о пользе:</b> современные модели и так неплохо переносят шум, поэтому очистка помогает в основном при заметном шипении или гуле; на чистой записи она почти ничего не меняет. Проверить на своём микрофоне просто: отправьте один сегмент с «Без обработки» и с «Сильная очистка» и сравните тексты в карточке сегмента.</div>
    ${presetChips("processing")}</div>
    <div class="card"><div class="fields">${fieldsHtml(PROCESSING_FIELDS, p, "processing")}</div></div>`;
}

/* ---------- microphone and general ---------- */

function micTab() {
  const mic = state.config.mic;
  const mics = state.micDevices;
  return `<div class="card"><div class="card-head"><h3>Микрофон</h3><button type="button" class="btn ghost sm" data-act="mics">Обновить список</button></div>
    <div class="field"><select data-act="mic-device"><option value="">Системный по умолчанию</option>${mics
      .map((m) => `<option value="${esc(m.id)}" ${m.id === mic.device_id ? "selected" : ""}>${esc(m.label)}</option>`)
      .join("")}</select></div></div>
    <div class="card"><div class="card-head"><h3>Обработка в браузере</h3></div>
    <div class="explain">По умолчанию всё выключено: шумоподавление и автоподстройка усиления браузера работают «вслепую» — они прячут тихую речь, «дышат» после громких слов и оставляют после себя цифровую тишину, из-за чего пауз не отличить. Вся обработка делается на сервере — над сегментами, с возможностью сравнить.</div>
    <label class="check"><input type="checkbox" data-act="mic-flag" data-flag="noise_suppression" ${mic.noise_suppression ? "checked" : ""}> шумоподавление браузера</label>
    <label class="check"><input type="checkbox" data-act="mic-flag" data-flag="auto_gain" ${mic.auto_gain ? "checked" : ""}> автоусиление браузера</label>
    <label class="check"><input type="checkbox" data-act="mic-flag" data-flag="echo_cancellation" ${mic.echo_cancellation ? "checked" : ""}> эхоподавление (нужно, только если звук идёт из колонок)</label>
    <div class="sub muted" style="font-size:.78rem">Изменения вступят в силу со следующей сессии.</div></div>`;
}

function generalTab() {
  const cfg = state.config;
  return `<div class="card"><div class="card-head"><h3>Текст</h3></div>
    <div class="field"><label class="lbl"><span>Пауза, с которой начинается новый абзац</span><span class="val" data-val="gap">${cfg.paragraph_gap_s} с</span></label>
      <input type="range" data-act="gap" min="0.5" max="8" step="0.1" value="${cfg.paragraph_gap_s}">
      <div class="sub">Сегменты, между которыми тишина короче, склеиваются в один абзац; длиннее — разделяются пустой строкой.</div></div></div>
    <div class="card"><div class="card-head"><h3>Файлы</h3></div>
    <div class="explain">Записи лежат в <span class="mono">${esc(cfg.recordings_dir)}</span>, модели — в <span class="mono">${esc(cfg.models_dir)}</span>.</div></div>`;
}

/* ---------- drawer events ---------- */

drawer.addEventListener("click", (event) => {
  const tabBtn = event.target.closest("[data-tab]");
  if (tabBtn) {
    tab = tabBtn.dataset.tab;
    return renderDrawer();
  }
  const el = event.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;
  if (act === "close") return closeSettings();
  if (act === "targets") return void guard(() => save({ targets: JSON.parse(el.dataset.t) }).then(renderDrawer));
  if (act === "preset") {
    const kind = el.dataset.kind;
    const preset = state.presets[kind][el.dataset.preset];
    return void guard(() => save({ [kind]: preset.params, [`${kind}_preset`]: el.dataset.preset }).then(renderDrawer));
  }
  if (act === "download") return void guard(() => api("/providers/gigaam/download", { method: "POST", body: { model: el.dataset.model } }));
  if (act === "warm") return void guard(async () => { await api("/providers/gigaam/warm", { method: "POST" }); setStatus("Модель загружается в память…", "ok", null, 2500); });
  if (act === "mics") return void guard(async () => { state.micDevices = await listMics(true); renderDrawer(); });
});

drawer.addEventListener("input", (event) => {
  const el = event.target;
  if (el.type === "range" && el.dataset.ns) {
    const defs = el.dataset.ns === "slicing" ? SLICING_FIELDS : PROCESSING_FIELDS;
    const def = defs.find((d) => d.key === el.dataset.key);
    const value = readField(el, defs);
    drawer.querySelector(`[data-val="${el.dataset.ns}.${el.dataset.key}"]`).textContent = fieldValue(def, value);
    state.config[el.dataset.ns][el.dataset.key] = value;
    const kind = el.dataset.ns;
    const name = matchingPreset(state.presets[kind], state.config[kind]);
    saveSoon({ [kind]: { [el.dataset.key]: value }, [`${kind}_preset`]: name });
  } else if (el.dataset.act === "gap") {
    drawer.querySelector('[data-val="gap"]').textContent = `${el.value} с`;
    saveSoon({ paragraph_gap_s: Number(el.value) });
  }
});

drawer.addEventListener("change", (event) => {
  const el = event.target;
  const act = el.dataset.act;
  if (el.type === "checkbox" && el.dataset.ns) {
    const defs = el.dataset.ns === "slicing" ? SLICING_FIELDS : PROCESSING_FIELDS;
    const value = readField(el, defs);
    const kind = el.dataset.ns;
    state.config[kind][el.dataset.key] = value;
    return void guard(() => save({ [kind]: { [el.dataset.key]: value }, [`${kind}_preset`]: matchingPreset(state.presets[kind], state.config[kind]) }).then(renderDrawer));
  }
  if (act === "pick-model") return void guard(() => save({ gigaam: { model: el.value } }).then(renderDrawer));
  if (act === "giga-device") return void guard(() => save({ gigaam: { device: el.value } }));
  if (act === "giga-threads") return void guard(() => save({ gigaam: { threads: Number(el.value) || 0 } }));
  if (act === "mic-device") return void guard(() => save({ mic: { device_id: el.value } }));
  if (act === "mic-flag") return void guard(() => save({ mic: { [el.dataset.flag]: el.checked } }));
});

scrim.addEventListener("click", () => {
  if (!$("modal").classList.contains("hidden")) closeModal();
  else closeSettings();
});

window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("modal").classList.contains("hidden")) closeModal();
  else if (!drawer.classList.contains("hidden")) closeSettings();
});

on("providers", () => {
  if (!drawer.classList.contains("hidden") && tab === "providers") renderDrawer();
});
on("open-settings", openSettings);

/* =============================== re-cut dialog =============================== */

const modal = $("modal");
let cut = null;

function closeModal() {
  modal.classList.add("hidden");
  modal.innerHTML = "";
  scrim.classList.add("hidden");
  cut = null;
}

export async function openResegment(sid) {
  const s = session(sid);
  if (!s) return;
  const key = `${state.recId}/${sid}`;
  // A session that was never cut (slicing off, e.g. recorded by v1) starts from the user's own defaults.
  const fresh = !s.slicing?.enabled;
  const slicing = fresh ? { ...state.config.slicing, enabled: true } : { ...s.slicing };
  cut = {
    sid, slicing, preset: matchingPreset(state.presets.slicing, slicing),
    processing: s.source === "migrated" ? state.config.processing_preset : s.processing_label === "custom" ? "" : "",
    targets: [...(state.config.targets || [])], regions: null, summary: null, busy: false, token: 0,
  };
  modal.classList.remove("hidden");
  drawModal();
  if (!state.peaks[key] && s.has_audio) {
    try {
      state.peaks[key] = await api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/peaks?buckets=1600`);
    } catch { /* the preview works without a waveform */ }
  }
  preview();
}

function drawModal() {
  const s = session(cut.sid);
  const presets = state.presets.slicing;
  const procOptions = Object.entries(state.presets.processing)
    .map(([k, v]) => `<option value="${k}" ${cut.processing === k ? "selected" : ""}>${esc(v.label)}</option>`)
    .join("");
  modal.innerHTML = `<div class="modal-card">
    <div class="panel-head"><h2>Пересобрать сегменты · сессия ${esc(s.id)}</h2><button type="button" class="btn ghost" data-act="m-close">✕</button></div>
    <div class="explain">Сессия записана целиком и не меняется. Здесь вы выбираете, как нарезать её заново. Текущие сегменты (вместе с их текстами) уберутся в папку <span class="mono">_archive</span> внутри сессии, а не пропадут.</div>
    <div>
      <canvas id="cutWave" class="preview-wave"></canvas>
      <div id="cutStats" class="stats" style="margin-top:.5rem"></div>
    </div>
    <div class="presets">${Object.entries(presets).map(([id, p]) => `<button type="button" class="chip ${id === cut.preset ? "on" : ""}" data-act="m-preset" data-preset="${id}">${esc(p.label)}</button>`).join("")}${cut.preset === "custom" ? `<span class="chip on">Свои настройки</span>` : ""}</div>
    <div class="fields">${fieldsHtml(SLICING_FIELDS, cut.slicing, "cut")}</div>
    <div class="fields">
      <div class="field"><label class="lbl"><span>Обработка звука</span></label>
        <select data-act="m-proc"><option value="" ${!cut.processing ? "selected" : ""}>как в сессии (${esc(state.presets.processing[s.processing_label]?.label || "свои настройки")})</option>${procOptions}</select></div>
      <div class="field"><label class="lbl"><span>Сразу отправить</span></label>
        <div class="seg-ctl">${TARGET_CHOICES.map(({ label, list }) => `<button type="button" data-act="m-target" data-t='${JSON.stringify(list)}' class="${list.length === cut.targets.length && list.every((x) => cut.targets.includes(x)) ? "on" : ""}">${label}</button>`).join("")}</div></div>
    </div>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn ghost" data-act="m-close">Отмена</button>
      <button type="button" class="btn primary" data-act="m-apply" ${cut.busy || !cut.summary ? "disabled" : ""}>Пересобрать${cut.summary ? ` · ${cut.summary.count} сегм.` : ""}</button></div>
  </div>`;
  scrim.classList.remove("hidden");
  paintCut();
}

function paintCut() {
  const canvas = $("cutWave");
  if (!canvas || !cut) return;
  const s = session(cut.sid);
  const data = state.peaks[`${state.recId}/${cut.sid}`];
  drawWave(canvas, { peaks: data?.peaks, duration: data?.duration || s.duration_seconds, segments: [], regions: cut.regions });
  const sum = cut.summary;
  $("cutStats").innerHTML = sum
    ? `<span>сегментов: <b>${sum.count}</b></span><span>длина: <b>${sum.min_s}–${sum.max_s} с</b>, медиана <b>${sum.median_s} с</b></span><span>в сегментах: <b>${fmtDur(sum.kept_s)}</b></span><span>выброшено тишины: <b>${fmtDur(sum.dropped_s)}</b></span>${sum.forced ? `<span style="color:var(--warn)">принудительных разрезов: <b>${sum.forced}</b></span>` : ""}`
    : `<span class="muted">считаю…</span>`;
}

const previewSoon = debounce(() => preview(), 500);

async function preview() {
  if (!cut) return;
  const mine = ++cut.token;
  cut.summary = null;
  paintCut();
  try {
    const res = await api(`/recordings/${enc(state.recId)}/sessions/${enc(cut.sid)}/resegment`, { method: "POST", body: { slicing: cut.slicing, dry_run: true } });
    if (!cut || mine !== cut.token) return;
    cut.regions = res.segments;
    cut.summary = res.summary;
    const apply = modal.querySelector('[data-act="m-apply"]');
    if (apply) {
      apply.disabled = false;
      apply.textContent = `Пересобрать · ${res.summary.count} сегм.`;
    }
    paintCut();
  } catch (error) {
    if (cut && mine === cut.token) setStatus(error.message, "error");
  }
}

modal.addEventListener("click", (event) => {
  if (event.target === modal) return closeModal();
  const el = event.target.closest("[data-act]");
  if (!el || !cut) return;
  const act = el.dataset.act;
  if (act === "m-close") return closeModal();
  if (act === "m-preset") {
    cut.slicing = { ...state.presets.slicing[el.dataset.preset].params };
    cut.preset = el.dataset.preset;
    drawModal();
    return preview();
  }
  if (act === "m-target") {
    cut.targets = JSON.parse(el.dataset.t);
    return drawModal();
  }
  if (act === "m-apply") {
    cut.busy = true;
    return void guard(async () => {
      try {
        await api(`/recordings/${enc(state.recId)}/sessions/${enc(cut.sid)}/resegment`, {
          method: "POST",
          body: { slicing: cut.slicing, targets: cut.targets, ...(cut.processing ? { processing_preset: cut.processing } : {}) },
        });
        closeModal();
        setStatus("Сессия пересобирается — сегменты появятся ниже", "ok");
      } finally {
        if (cut) cut.busy = false;
      }
    });
  }
});

modal.addEventListener("input", (event) => {
  const el = event.target;
  if (el.type !== "range" || !cut) return;
  const def = SLICING_FIELDS.find((d) => d.key === el.dataset.key);
  const value = readField(el, SLICING_FIELDS);
  modal.querySelector(`[data-val="cut.${el.dataset.key}"]`).textContent = fieldValue(def, value);
  cut.slicing[el.dataset.key] = value;
  cut.preset = matchingPreset(state.presets.slicing, cut.slicing);
  previewSoon();
});

modal.addEventListener("change", (event) => {
  const el = event.target;
  if (el.dataset.act === "m-proc") cut.processing = el.value;
});

on("open-resegment", openResegment);
