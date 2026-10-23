import { api, enc } from "./api.js";
import { renameRecording, refreshLibrary, renderLibrary } from "./library.js";
import { isRecording, listMics, startSession, stopSession } from "./recorder.js";
import { emit, modelMissing, on, providerReady, segment, session, state } from "./state.js";
import {
  $, CUT_REASONS, PROVIDERS, PROVIDER_IDS, TARGET_CHOICES, WARNINGS, archivedIds, copyText, esc, fmtClock, fmtDate, fmtDur, guard, isArchived,
  processingLabel, providerInfo, setStatus, store, targetChoiceId,
} from "./util.js";
import { attachWave } from "./waveform.js";

const els = {
  recView: $("recView"), empty: $("emptyState"), title: $("recTitle"), ctx: $("ctxInfo"),
  recorder: $("recorder"), recBtn: $("recBtn"), recBtnLabel: $("recBtnLabel"), timer: $("timer"),
  meterFill: $("meterFill"), markFloor: $("markFloor"), markDip: $("markDip"), meterText: $("meterText"),
  meterState: $("meterState"), meterLegend: $("meterLegend"), liveCount: $("liveCount"), notice: $("recNotice"),
  targetCtl: $("targetCtl"), targetHint: $("targetHint"), slicingPreset: $("slicingPreset"), slicingHint: $("slicingHint"),
  processingPreset: $("processingPreset"), processingHint: $("processingHint"), micSelect: $("micSelect"),
  transcript: $("transcript"), viewCtl: $("viewCtl"), sessions: $("sessions"),
};

const METER_MIN_DB = -70;
const meterPos = (db) => `${Math.max(0, Math.min(100, ((db - METER_MIN_DB) / -METER_MIN_DB) * 100))}%`;
let saving = false;
const waves = new Map(); // session id -> {canvas, redraw}

/* =============================== opening a recording =============================== */

export async function openRecording(id, { keepSelection = false } = {}) {
  const data = await api(`/recordings/${enc(id)}`);
  state.recId = data.id;
  state.rec = data;
  if (!keepSelection) state.selected = null;
  store("rec", id);
  for (const s of data.sessions) if (state.open[s.id] === undefined) state.open[s.id] = false;
  const newest = data.sessions.at(-1);
  if (newest && !keepSelection) state.open[newest.id] = true;
  renderAll();
  renderLibrary();
  emit("recording-opened");
}

export function reloadRecording() {
  return state.recId ? openRecording(state.recId, { keepSelection: true }) : Promise.resolve();
}

function renderAll() {
  const has = !!state.rec;
  els.recView.classList.toggle("hidden", !has);
  els.empty.classList.toggle("hidden", has);
  if (!has) {
    els.ctx.textContent = "Запись не выбрана";
    return;
  }
  renderHeader();
  renderRecorder();
  renderTranscript();
  renderSessions();
}

function renderHeader() {
  const rec = state.rec;
  const title = rec.meta.title || rec.id;
  els.title.textContent = title;
  const segs = rec.sessions.reduce((n, s) => n + (s.segment_count || 0), 0);
  const dur = rec.sessions.reduce((n, s) => n + (s.duration_seconds || 0), 0);
  els.ctx.innerHTML = `<span class="name">${esc(title)}</span><span>${rec.sessions.length} сесс.</span><span>${segs} сегм.</span><span>${fmtDur(dur)}</span>`;
  $("copyBtn").disabled = !rec.transcript.trim();
}

/* =============================== recorder panel =============================== */

export function renderRecorder() {
  const cfg = state.config;
  if (!cfg || !state.rec) return;
  const live = isRecording();

  els.recorder.classList.toggle("live", live);
  els.recBtn.classList.toggle("stop", live);
  els.recBtn.disabled = saving;
  els.recBtnLabel.textContent = saving ? "Сохранение…" : live ? "Завершить сессию" : "Начать сессию";
  if (!live) {
    els.timer.textContent = "00:00";
    els.meterFill.style.width = "0";
    els.markFloor.classList.add("hidden");
    els.markDip.classList.add("hidden");
    els.meterLegend.classList.add("hidden");
    els.liveCount.textContent = "";
    els.meterText.textContent = saving ? "сохраняю последние секунды…" : "микрофон выключен";
    els.meterState.firstElementChild.className = "dot";
  }

  const current = targetChoiceId(cfg.targets);
  els.targetCtl.innerHTML = TARGET_CHOICES.map((t) => `<button type="button" data-t="${t.id}" class="${t.id === current ? "on" : ""}" ${live ? "disabled" : ""}>${t.label}</button>`).join("");
  const warn = cfg.targets.filter((p) => !providerReady(p)).map((p) => `${PROVIDERS[p].name}: ${state.providers[p]?.message || "недоступна"}`);
  // a missing model is fixed right here (the setup dialog); any other trouble is explained in the settings
  const fix = modelMissing() ? `<span class="link" data-open-model>скачать модель</span>` : `<span class="link" data-open-settings="providers">настроить</span>`;
  els.targetHint.innerHTML = warn.length
    ? `<span style="color:var(--warn)">⚠ ${esc(warn.join(" · "))}</span> ${fix}`
    : cfg.targets.length
      ? "Сегмент уходит на расшифровку сразу после разреза, пока вы говорите дальше."
      : "Сегменты только запишутся; отправить можно позже, любой моделью.";

  fillPresetSelect(els.slicingPreset, state.presets.slicing, cfg.slicing_preset, live);
  fillPresetSelect(els.processingPreset, state.presets.processing, cfg.processing_preset, live);
  const s = cfg.slicing;
  els.slicingHint.textContent = s.enabled
    ? `сегмент ${s.target_len_s}–${s.max_len_s} с · пауза ${(s.pause_ms / 1000).toFixed(2)} с`
    : "вся сессия одним куском";
  const p = cfg.processing;
  els.processingHint.textContent =
    [p.highpass_hz ? `срез ${p.highpass_hz} Гц` : "", p.denoise ? `шум −${Math.round(p.denoise * 100)}%` : "", p.normalize ? `громкость ${p.target_dbfs} дБ` : ""]
      .filter(Boolean)
      .join(" · ") || "звук уходит как записан";

  const mics = state.micDevices;
  const deviceId = cfg.mic.device_id;
  els.micSelect.disabled = live;
  els.micSelect.innerHTML =
    `<option value="">Системный по умолчанию</option>` +
    mics.map((m) => `<option value="${esc(m.id)}" ${m.id === deviceId ? "selected" : ""}>${esc(m.label)}</option>`).join("") +
    (mics.some((m) => !m.label.startsWith("Микрофон ")) ? "" : `<option value="__ask__">Показать устройства (нужен доступ)…</option>`);
  if (deviceId && mics.length && !mics.some((m) => m.id === deviceId)) els.micSelect.value = "";

  const notices = [];
  if (!window.isSecureContext) notices.push(`<div class="notice bad">Микрофон работает только на адресе http://127.0.0.1:8765 или localhost — сейчас страница открыта иначе.</div>`);
  els.notice.innerHTML = notices.join("");
}

function fillPresetSelect(select, presets, current, disabled) {
  const names = Object.keys(presets);
  select.disabled = disabled;
  select.innerHTML =
    names.map((n) => `<option value="${n}" ${n === current ? "selected" : ""}>${esc(presets[n].label)}</option>`).join("") +
    (current === "custom" || !names.includes(current) ? `<option value="custom" selected>Свои настройки</option>` : "");
}

els.targetCtl.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-t]");
  if (!btn || isRecording()) return;
  const targets = TARGET_CHOICES.find((t) => t.id === btn.dataset.t).list;
  guard(async () => {
    const res = await api("/config", { method: "PUT", body: { targets } });
    state.config = res.config;
    renderRecorder();
  });
});

for (const [select, key] of [[els.slicingPreset, "slicing"], [els.processingPreset, "processing"]]) {
  select.addEventListener("change", () => {
    const name = select.value;
    const preset = state.presets[key][name];
    if (!preset) return;
    guard(async () => {
      const res = await api("/config", { method: "PUT", body: { [key]: preset.params, [`${key}_preset`]: name } });
      state.config = res.config;
      renderRecorder();
    });
  });
}

els.micSelect.addEventListener("change", () => {
  const value = els.micSelect.value;
  guard(async () => {
    if (value === "__ask__") {
      state.micDevices = await listMics(true);
      renderRecorder();
      return;
    }
    const res = await api("/config", { method: "PUT", body: { mic: { device_id: value } } });
    state.config = res.config;
    renderRecorder();
  });
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-open-settings]");
  if (link) emit("open-settings", link.dataset.openSettings);
});
$("moreSettings").addEventListener("click", () => emit("open-settings", "slicing"));

els.recBtn.addEventListener("click", () => guard(toggleRecording));

async function toggleRecording() {
  if (isRecording()) {
    saving = true;
    renderRecorder();
    try {
      await stopSession();
    } finally {
      saving = false;
    }
    await reloadRecording();
    renderRecorder();
    emit("library-dirty");
    return;
  }
  const cfg = state.config;
  const unready = cfg.targets.filter((p) => !providerReady(p));
  if (unready.length) {
    const p = unready[0];
    const download = modelMissing();
    setStatus(
      `${PROVIDERS[p].name}: ${state.providers[p]?.message || "недоступна"}. ${download ? "Скачайте модель или выберите «Не отправлять»." : "Выберите другую модель или «Не отправлять»."}`,
      "error",
      download
        ? { label: "Скачать модель", onClick: () => emit("open-model-setup") }
        : { label: "Настроить", onClick: () => emit("open-settings", "providers") },
    );
    return;
  }
  els.recBtn.disabled = true;
  try {
    const started = await startSession({
      recordingId: state.recId, targets: cfg.targets, slicing: cfg.slicing, processing: cfg.processing, mic: cfg.mic, language: cfg.language,
    });
    state.open[started.id] = true;
  } finally {
    els.recBtn.disabled = false;
  }
  renderRecorder();
}

on("live", ({ active, saving: isSaving }) => {
  if (active) {
    els.meterLegend.classList.remove("hidden");
    els.meterText.textContent = "слушаю…";
  }
  if (isSaving) saving = true;
  renderRecorder();
  renderSessions();
});

on("live-level", ({ rms }) => {
  const db = 20 * Math.log10(Math.max(rms, 1e-5));
  els.meterFill.style.width = meterPos(db);
});

on("live-tick", (sec) => {
  els.timer.textContent = fmtClock(sec);
});

on("live-info", (info) => {
  if (info.floor !== null && info.floor !== undefined) {
    els.markFloor.style.left = meterPos(info.floor);
    els.markFloor.classList.remove("hidden");
    els.markDip.style.left = meterPos(info.dip);
    els.markDip.classList.remove("hidden");
    const speaking = info.db >= info.threshold;
    els.meterState.firstElementChild.className = `dot ${speaking ? "ok" : ""}`;
    els.meterText.textContent = speaking ? "речь" : "пауза";
  }
  els.liveCount.textContent = `сегментов: ${info.segments}`;
});

on("live-error", (message) => setStatus(message, "error"));

window.addEventListener("beforeunload", (event) => {
  if (isRecording()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

/* =============================== header actions =============================== */

els.title.addEventListener("dblclick", () => {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rec-title-input";
  input.value = state.rec.meta.title;
  els.title.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    input.replaceWith(els.title);
    if (save && value && value !== state.rec.meta.title) {
      await guard(async () => {
        const meta = await renameRecording(state.recId, value);
        state.rec.meta = meta;
        renderHeader();
        await refreshLibrary();
      });
    }
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
});

$("copyBtn").addEventListener("click", async () => {
  const ok = await copyText(shownTranscript());
  setStatus(ok ? "Текст скопирован" : "Не удалось скопировать", ok ? "ok" : "error", null, 2500);
});

$("folderBtn").addEventListener("click", () => guard(() => api(`/recordings/${enc(state.recId)}/open`, { method: "POST" })));

/* =============================== transcript =============================== */

// "Архив" is offered only for recordings that still carry transcripts of a provider this version no longer ships.
function views() {
  const hasArchive = state.rec?.sessions.some((s) => s.segments.some((g) => archivedIds(g).length));
  return [
    { id: "auto", label: "Авто" },
    { id: "gigaam", label: "GigaAM" },
    ...(hasArchive ? [{ id: "archive", label: "Архив" }] : []),
  ];
}

function ownTranscript(g, view) {
  if (view !== "archive") return g.transcripts?.[view];
  const id = archivedIds(g).find((p) => g.transcripts[p].status === "done");
  return id ? g.transcripts[id] : undefined;
}

function shownTranscript() {
  const rec = state.rec;
  if (!rec) return "";
  if (state.view === "auto") return rec.transcript.trim();
  const gap = state.config.paragraph_gap_s;
  const parts = rec.sessions.map((s) => {
    let out = "";
    s.segments.forEach((g, i) => {
      const own = ownTranscript(g, state.view);
      const text = (own?.status === "done" ? own.text : g.text || "").trim();
      if (!text) return;
      if (out) out += (i > 0 && (g.gap_before_s || 0) >= gap ? "\n\n" : " ");
      out += text;
    });
    return out;
  });
  return parts.filter(Boolean).join("\n\n");
}

export function renderTranscript() {
  if (!state.rec) return;
  const available = views();
  if (!available.some((v) => v.id === state.view)) state.view = "auto";
  els.viewCtl.innerHTML = available.map((v) => `<button type="button" data-v="${v.id}" class="${v.id === state.view ? "on" : ""}">${v.label}</button>`).join("");
  const text = shownTranscript();
  els.transcript.textContent = text || "Текст появится, когда первые сегменты будут расшифрованы.";
  els.transcript.classList.toggle("empty", !text);
  $("copyBtn").disabled = !text;
}

els.viewCtl.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-v]");
  if (!btn) return;
  state.view = btn.dataset.v;
  renderTranscript();
});

on("transcript", renderTranscript);

/* =============================== sessions =============================== */

function warningBadges(g) {
  return (g.audio?.warnings || [])
    .map((w) => `<span class="badge warn" title="${esc(WARNINGS[w]?.text || w)}">${esc(WARNINGS[w]?.short || w)}</span>`)
    .join("");
}

function provButton(g, pid) {
  const t = g.transcripts?.[pid];
  const status = t?.status;
  const p = providerInfo(pid);
  let cls = "idle";
  let label = p.short;
  let title = `${p.name}: отправить на расшифровку`;
  if (isArchived(pid)) {
    // read-only: nothing is ever sent to an archived provider, the button only opens the segment
    cls = status === "done" ? `done ${g.provider === pid ? "picked" : ""}` : "error";
    label = `${status === "done" ? "✓" : "✗"}${p.short}`;
    title = `${p.full}: ${status === "done" ? `готово${g.provider === pid ? " (этот текст показан)" : ""}` : "была ошибка"}. Нажмите, чтобы открыть`;
  } else if (status === "queued" || status === "running") {
    cls = "working";
    label = `<span class="spinner" style="width:.6rem;height:.6rem"></span>${p.short}`;
    title = `${p.name}: ${status === "queued" ? "в очереди" : "распознаю…"}`;
  } else if (status === "done") {
    cls = `done ${g.provider === pid ? "picked" : ""}`;
    label = `✓${p.short}`;
    title = `${p.name}: готово${g.provider === pid ? " (этот текст показан)" : ""}. Нажмите, чтобы открыть результат`;
  } else if (status === "error") {
    cls = "error";
    label = `✗${p.short}`;
    title = `${p.name}: ошибка — ${t.error?.message || ""}. Нажмите, чтобы отправить заново`;
  }
  return `<button type="button" class="pbtn ${cls}" data-act="prov" data-p="${pid}" title="${esc(title)}" ${status === "queued" || status === "running" ? "disabled" : ""}>${label}</button>`;
}

function rowHtml(s, g) {
  const sel = state.selected?.sid === s.id && state.selected?.gid === g.id;
  let text;
  if (g.text) text = `<div class="seg-text">${esc(g.text)}</div>`;
  else {
    const failed = Object.entries(g.transcripts || {}).find(([, t]) => t.status === "error");
    if (failed) text = `<div class="seg-text err">${esc(providerInfo(failed[0]).name)}: ${esc(failed[1].error?.message || "ошибка")}</div>`;
    else if (g.state === "working") text = `<div class="seg-text none">распознаю…</div>`;
    else if (g.state === "done") text = `<div class="seg-text none">модель вернула пустой ответ — речи в сегменте не услышано</div>`;
    else text = `<div class="seg-text none">не отправлен на расшифровку</div>`;
  }
  return `<div class="seg-row ${sel ? "selected" : ""}" data-sid="${s.id}" data-gid="${g.id}">
    <span class="idx">${esc(g.id)}</span>
    <span class="when">${fmtClock(g.start_s)}–${fmtClock(g.end_s)}</span>
    <span class="len">${(g.duration_seconds || 0).toFixed(1)} с</span>
    ${text}
    <span class="seg-prov">${[...PROVIDER_IDS, ...archivedIds(g)].map((p) => provButton(g, p)).join("")}</span>
    <span class="seg-tools">${warningBadges(g)}<button type="button" class="tbtn" data-act="play" title="Прослушать (после обработки)">▶</button></span>
  </div>`;
}

function sessionTitleBadges(s) {
  const slicing = state.presets.slicing[s.slicing_label]?.label || (s.slicing_label === "custom" ? "свои" : s.slicing_label);
  const out = [];
  if (s.status === "recording") out.push(`<span class="badge bad"><span class="dot bad"></span>идёт запись</span>`);
  if (s.status === "processing") out.push(`<span class="badge info"><span class="spinner" style="width:.55rem;height:.55rem"></span>нарезка</span>`);
  if (s.source === "migrated") out.push(`<span class="badge info" title="Записана прежней версией программы (с браузерным шумоподавлением и автоусилением). Звук сохранён как был.">из прежней версии</span>`);
  if (s.recovered) out.push(`<span class="badge warn" title="Программа закрылась во время записи; звук восстановлен, но последние секунды могли не попасть в сегменты. Используйте «Пересобрать».">восстановлена</span>`);
  out.push(`<span class="badge" title="Нарезка">${esc(slicing)}</span>`);
  out.push(`<span class="badge" title="Обработка звука">${esc(processingLabel(s.processing_label, state.presets))}</span>`);
  const errors = s.segments.filter((g) => g.state === "error").length;
  const working = s.segments.filter((g) => g.state === "working").length;
  if (errors) out.push(`<span class="badge bad" title="Сегментов с ошибкой">⚠ ${errors}</span>`);
  if (working) out.push(`<span class="badge info" title="Сегментов ждут расшифровки или распознаются">⏳ ${working}</span>`);
  return out.join("");
}

function sessionNotices(s) {
  if (s.status !== "ready") return "";
  const out = [];
  if (s.slicing?.enabled && s.segment_count === 0 && (s.duration_seconds || 0) > 1) {
    out.push(`<div class="notice bad">В этой сессии не найдено речи (${fmtDur(s.duration_seconds)} записи). Проверьте микрофон. Если речь была тихой — «Пересобрать…» с большей чувствительностью. Запись звука сохранена.</div>`);
  } else if (s.slicing?.enabled && s.duration_seconds > 20 && s.kept_seconds / s.duration_seconds < 0.25) {
    out.push(`<div class="notice">В сегменты попало лишь ${Math.round((s.kept_seconds / s.duration_seconds) * 100)}% записи, остальное — паузы. Если речи было больше — «Пересобрать…» с большей чувствительностью.</div>`);
  }
  if (s.error) out.push(`<div class="notice bad">${esc(s.error)}</div>`);
  return out.join("");
}

function sendBarHtml(s) {
  const bar = state.sendBar[s.id];
  if (!bar?.open) return "";
  // with a single model there is nothing to choose, so the chips only appear once there are several
  const chips = PROVIDER_IDS.length < 2 ? "" : PROVIDER_IDS.map(
    (p) => `<button type="button" class="chip ${bar.providers.includes(p) ? "on" : ""}" data-act="bar-prov" data-p="${p}">${PROVIDERS[p].name}${providerReady(p) ? "" : " ⚠"}</button>`
  ).join("");
  const proc = Object.entries(state.presets.processing)
    .map(([k, v]) => `<option value="${k}" ${bar.processing === k ? "selected" : ""}>${esc(v.label)}</option>`)
    .join("");
  return `<div class="row" style="padding:.5rem .9rem .6rem;border-top:1px solid var(--line);background:rgba(8,14,10,.3)">
    <span class="mono muted" style="font-size:.74rem">Отправить${chips ? ":" : " в GigaAM:"}</span>${chips}
    <select data-act="bar-scope">
      <option value="all" ${bar.only === "all" ? "selected" : ""}>все сегменты</option>
      <option value="missing" ${bar.only === "missing" ? "selected" : ""}>без готового текста</option>
      <option value="errors" ${bar.only === "errors" ? "selected" : ""}>только с ошибкой</option>
    </select>
    <select data-act="bar-proc"><option value="" ${!bar.processing ? "selected" : ""}>обработка как записана</option>${proc}</select>
    <button type="button" class="btn primary sm" data-act="bar-send" ${bar.providers.length ? "" : "disabled"}>Отправить</button>
  </div>`;
}

function sessionHeadHtml(s) {
  const live = s.status === "recording";
  const dur = live ? state.live?.elapsed ?? 0 : s.duration_seconds || 0;
  const busy = s.status !== "ready";
  return `<span class="caret">▸</span>
    <span class="session-title">Сессия ${esc(s.id)}</span>
    <span class="session-meta"><span>${esc(fmtDate(s.started_at))}</span><span>${fmtClock(dur)}</span><span>${s.segment_count || 0} сегм.</span>${sessionTitleBadges(s)}</span>
    <span class="session-actions">
      <button type="button" class="btn ghost sm" data-act="bar" ${busy ? "disabled" : ""}>Отправить на расшифровку ▾</button>
      <button type="button" class="btn ghost sm" data-act="resegment" ${busy ? "disabled" : ""}>Пересобрать…</button>
      <button type="button" class="btn ghost sm" data-act="folder">Папка</button>
    </span>`;
}

function sessionHtml(s) {
  const open = !!state.open[s.id];
  const live = s.status === "recording";
  return `<article class="panel session ${open ? "open" : ""} ${live ? "live" : ""}" data-sid="${s.id}">
    <div class="session-head" data-act="toggle">${sessionHeadHtml(s)}</div>
    <div class="sendbar">${sendBarHtml(s)}</div>
    <div class="session-body">
      <div class="notices">${sessionNotices(s)}</div>
      <div class="wave-wrap">
        <div class="wave-tip hidden"></div>
        <canvas class="wave" aria-label="Звук сессии"></canvas>
        <div class="wave-legend">
          <span><i style="background:rgb(154,175,156)"></i>не отправлен</span><span><i style="background:rgb(106,168,201)"></i>в работе</span>
          <span><i style="background:rgb(111,191,138)"></i>готов</span><span><i style="background:rgb(210,101,79)"></i>ошибка</span>
          <span>тёмное — паузы, которые не попали в сегменты</span>
        </div>
      </div>
      <div class="seg-list">${s.segments.map((g) => rowHtml(s, g)).join("") || `<p class="muted" style="padding:.3rem .2rem;font-size:.85rem">${s.status === "recording" ? "Сегменты появятся здесь, как только программа найдёт паузу…" : "Сегментов нет."}</p>`}</div>
    </div>
  </article>`;
}

export function renderSessions() {
  if (!state.rec) return;
  waves.clear();
  const list = [...state.rec.sessions].reverse();
  els.sessions.innerHTML = list.length
    ? list.map(sessionHtml).join("")
    : `<section class="panel empty-state" style="padding:1.4rem"><p>Сессий пока нет. Нажмите «Начать сессию» и говорите — программа сама нарежет речь на сегменты.</p></section>`;
  for (const s of state.rec.sessions) if (state.open[s.id]) ensureWave(s.id);
}

function sessionEl(sid) {
  return els.sessions.querySelector(`.session[data-sid="${sid}"]`);
}

function refreshSessionChrome(sid) {
  const s = session(sid);
  const el = sessionEl(sid);
  if (!s || !el) return;
  el.classList.toggle("live", s.status === "recording");
  el.querySelector(".session-head").innerHTML = sessionHeadHtml(s);
  el.querySelector(".notices").innerHTML = sessionNotices(s);
  el.querySelector(".sendbar").innerHTML = sendBarHtml(s);
}

function refreshSessionHead(sid) {
  const s = session(sid);
  const el = sessionEl(sid);
  if (!s || !el) return;
  el.querySelector(".session-head").innerHTML = sessionHeadHtml(s);
  el.querySelector(".notices").innerHTML = sessionNotices(s);
}

function ensureWave(sid) {
  const s = session(sid);
  const el = sessionEl(sid);
  if (!s || !el || waves.has(sid)) {
    waves.get(sid)?.redraw();
    return;
  }
  const canvas = el.querySelector(".wave");
  const key = `${state.recId}/${sid}`;
  const getData = () => ({
    peaks: session(sid)?.status === "recording" ? state.live?.peaks : state.peaks[key]?.peaks,
    duration: session(sid)?.status === "recording" ? Math.max(state.live?.elapsed || 0, 1) : state.peaks[key]?.duration || session(sid)?.duration_seconds || 0,
    segments: session(sid)?.segments || [],
    selectedGid: state.selected?.sid === sid ? state.selected.gid : null,
  });
  const wave = attachWave(canvas, el.querySelector(".wave-tip"), getData, { onPick: (seg) => selectSegment(sid, seg.id, { scroll: true }) });
  waves.set(sid, wave);
  wave.redraw();
  if (s.has_audio && !state.peaks[key]) {
    api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/peaks?buckets=1600`)
      .then((data) => {
        state.peaks[key] = data;
        waves.get(sid)?.redraw();
      })
      .catch(() => {});
  }
}

function redrawWave(sid) {
  waves.get(sid)?.redraw();
}

on("segment", ({ sid, segment: g }) => {
  const s = session(sid);
  const el = sessionEl(sid);
  if (!s || !el) {
    if (s && state.rec) renderSessions();
    return;
  }
  const list = el.querySelector(".seg-list");
  const row = list.querySelector(`.seg-row[data-gid="${g.id}"]`);
  const html = rowHtml(s, g);
  if (row) row.outerHTML = html;
  else {
    if (!list.querySelector(".seg-row")) list.innerHTML = "";
    list.insertAdjacentHTML("beforeend", html);
  }
  redrawWave(sid);
  refreshSessionHead(sid);
});

on("session", (data) => {
  const el = sessionEl(data.id);
  if (!el) {
    if (data.status === "recording") state.open[data.id] = true;
    renderSessions();
    renderHeader();
    return;
  }
  refreshSessionChrome(data.id);
  if (data.segments) {
    el.querySelector(".seg-list").innerHTML = session(data.id).segments.map((g) => rowHtml(session(data.id), g)).join("");
  }
  if (data.has_audio && !state.peaks[`${state.recId}/${data.id}`]) ensureWave(data.id);
  redrawWave(data.id);
  renderHeader();
});

on("sessions", () => {
  renderSessions();
  renderHeader();
});

on("live-info", () => {
  const s = state.live?.sessionId;
  if (s) redrawWave(s);
});

on("providers", () => {
  renderRecorder();
});

/* ---------- selection and clicks ---------- */

export function selectSegment(sid, gid, { scroll = false, provider = null } = {}) {
  state.selected = { sid, gid, provider };
  for (const row of els.sessions.querySelectorAll(".seg-row.selected")) row.classList.remove("selected");
  const row = els.sessions.querySelector(`.seg-row[data-sid="${sid}"][data-gid="${gid}"]`);
  row?.classList.add("selected");
  if (scroll) {
    if (!state.open[sid]) toggleSession(sid, true);
    row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  for (const id of waves.keys()) redrawWave(id);
  emit("selection");
}

function toggleSession(sid, force) {
  const el = sessionEl(sid);
  if (!el) return;
  state.open[sid] = force ?? !state.open[sid];
  el.classList.toggle("open", state.open[sid]);
  if (state.open[sid]) ensureWave(sid);
}

let player = null;
let playing = null;
function stopPlayback() {
  if (player) player.pause();
  els.sessions.querySelectorAll(".seg-row.playing").forEach((r) => r.classList.remove("playing"));
  els.sessions.querySelectorAll(".tbtn.on").forEach((b) => {
    b.classList.remove("on");
    b.textContent = "▶";
  });
  playing = null;
}

export function playSegment(sid, gid, kind = "processed") {
  const key = `${sid}/${gid}/${kind}`;
  const wasPlaying = playing === key;
  stopPlayback();
  if (wasPlaying) return;
  player = player || new Audio();
  player.onended = stopPlayback;
  player.onerror = () => {
    stopPlayback();
    setStatus("Не удалось воспроизвести сегмент", "error");
  };
  player.src = `/api/recordings/${enc(state.recId)}/sessions/${enc(sid)}/segments/${enc(gid)}/audio?kind=${kind}&t=${Date.now()}`;
  player.play().catch(() => {});
  playing = key;
  const row = els.sessions.querySelector(`.seg-row[data-sid="${sid}"][data-gid="${gid}"]`);
  row?.classList.add("playing");
  const btn = row?.querySelector('[data-act="play"]');
  if (btn) {
    btn.classList.add("on");
    btn.textContent = "■";
  }
}

export async function sendSegment(sid, gid, providers, processingPreset = null) {
  await api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/segments/${enc(gid)}/transcribe`, {
    method: "POST",
    body: { providers, ...(processingPreset ? { preset: processingPreset } : {}) },
  });
}

els.sessions.addEventListener("click", (event) => {
  const target = event.target.closest("[data-act]");
  const sessionNode = event.target.closest(".session");
  if (!sessionNode) return;
  const sid = sessionNode.dataset.sid;
  const row = event.target.closest(".seg-row");

  if (row) {
    const gid = row.dataset.gid;
    const act = target?.dataset.act;
    if (act === "play") return playSegment(sid, gid);
    if (act === "prov") {
      const pid = target.dataset.p;
      const g = segment(sid, gid);
      const status = g?.transcripts?.[pid]?.status;
      if (status === "done" || isArchived(pid)) return selectSegment(sid, gid, { provider: pid });
      return void guard(async () => {
        await sendSegment(sid, gid, [pid]);
        selectSegment(sid, gid, { provider: pid });
      });
    }
    return selectSegment(sid, gid);
  }

  const act = target?.dataset.act;
  if (act === "folder") return void guard(() => api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/open`, { method: "POST" }));
  if (act === "resegment") return emit("open-resegment", sid);
  if (act === "bar") {
    const cfg = state.config;
    const bar = state.sendBar[sid] || { open: false, providers: cfg.targets.length ? [...cfg.targets] : ["gigaam"], only: "missing", processing: "" };
    bar.open = !bar.open;
    state.sendBar[sid] = bar;
    refreshSessionChrome(sid);
    return;
  }
  if (act === "bar-prov") {
    const bar = state.sendBar[sid];
    const pid = target.dataset.p;
    bar.providers = bar.providers.includes(pid) ? bar.providers.filter((p) => p !== pid) : [...bar.providers, pid];
    refreshSessionChrome(sid);
    return;
  }
  if (act === "bar-send") {
    const bar = state.sendBar[sid];
    return void guard(async () => {
      const res = await api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/transcribe`, {
        method: "POST",
        body: { providers: bar.providers, only: bar.only, ...(bar.processing ? { preset: bar.processing } : {}) },
      });
      setStatus(res.queued ? `В очередь поставлено: ${res.queued}` : "Нечего отправлять: все сегменты уже в работе или с готовым текстом", res.queued ? "ok" : "", null, 3500);
    });
  }
  if (act === "bar-scope" || act === "bar-proc") return;
  if (event.target.closest(".session-head")) toggleSession(sid);
});

els.sessions.addEventListener("change", (event) => {
  const sel = event.target.closest("select[data-act]");
  const sid = event.target.closest(".session")?.dataset.sid;
  if (!sel || !sid) return;
  const bar = state.sendBar[sid];
  if (sel.dataset.act === "bar-scope") bar.only = sel.value;
  if (sel.dataset.act === "bar-proc") bar.processing = sel.value;
});

on("selection", () => {
  // keep the played row marker consistent after re-renders
});

/* ---------- init ---------- */

export async function initWorkspace() {
  state.micDevices = await listMics(false).catch(() => []);
}
export { refreshSessionChrome };
