import { api, enc } from "./api.js";
import { selectSegment, sendSegment } from "./workspace.js";
import { emit, on, segment, selectedSegment, session, state } from "./state.js";
import {
  $, CUT_REASONS, PROVIDERS, PROVIDER_IDS, STAGES, WARNINGS, archivedIds, copyText, diffHtml, esc, fmtClock, fmtDate, fmtDb, guard, isArchived,
  processingLabel, providerInfo, setStatus,
} from "./util.js";

const root = $("inspector");
const shell = $("shell");
let shownKey = null; // which segment the static part (players) was rendered for
let audioStamp = 0;
let resend = { providers: null, preset: "", custom: false, params: { highpass_hz: 80, denoise: 0.5, normalize: true, target_dbfs: -20, max_gain_db: 30 } };
let diffOn = true;

const url = (sid, gid, kind) =>
  `/api/recordings/${enc(state.recId)}/sessions/${enc(sid)}/segments/${enc(gid)}/audio?kind=${kind}&t=${audioStamp}`;

function statRow(label, a, b, unit = "") {
  const f = (v) => (v === null || v === undefined ? "—" : `${v}${unit}`);
  return `<span class="k">${label}</span><span class="num">${f(a)}</span><span class="num">${f(b)}</span>`;
}

function statsHtml(g) {
  const o = g.audio?.original;
  const p = g.audio?.processed;
  if (!o) return `<p class="muted" style="font-size:.82rem">Статистика появится после подготовки звука.</p>`;
  const warnings = (g.audio.warnings || []).map((w) => `<div class="notice" style="font-size:.8rem">${esc(WARNINGS[w]?.text || w)}</div>`).join("");
  const steps = (g.audio.steps || [])
    .map((st) => {
      if (st.step === "highpass") return `<span class="badge">срез ${st.hz} Гц</span>`;
      if (st.step === "denoise") return st.applied ? `<span class="badge ok" title="Средний подавленный уровень ${st.mean_gain_db} дБ">шум −${Math.round(st.strength * 100)}%</span>` : `<span class="badge warn" title="${esc(st.skipped || "")}">шум не тронут</span>`;
      if (st.step === "normalize") return `<span class="badge">громкость ${st.gain_db > 0 ? "+" : ""}${st.gain_db} дБ</span>`;
      return `<span class="badge">${esc(st.step)}</span>`;
    })
    .join("");
  return `<div class="kv"><span class="h"></span><span class="h num">оригинал</span><span class="h num">обработан</span>
    ${statRow("речь, дБFS", o.speech_rms_dbfs, p?.speech_rms_dbfs)}
    ${statRow("шум, дБFS", o.noise_dbfs, p?.noise_dbfs)}
    ${statRow("запас над шумом, дБ", o.snr_db, p?.snr_db)}
    ${statRow("пик, дБFS", o.peak_dbfs, p?.peak_dbfs)}
    ${statRow("перегруз, %", o.clipped_ratio !== undefined ? +(o.clipped_ratio * 100).toFixed(2) : null, p?.clipped_ratio !== undefined ? +(p.clipped_ratio * 100).toFixed(2) : null)}
  </div>${steps ? `<div class="steps">${steps}</div>` : ""}${warnings}`;
}

function errorHtml(err, pid) {
  if (!err) return "";
  const rows = [
    ["Где", STAGES[err.stage] || err.stage],
    ["Тип", err.type],
    err.http_status ? ["HTTP", err.http_status] : null,
    ["Попыток", err.attempts],
    ["Когда", fmtDate(err.at)],
    ...Object.entries(err.context || {}).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : v]),
  ].filter(Boolean);
  return `<div class="err-box">
    <div class="msg">${esc(err.message)}</div>
    ${err.hint ? `<div class="hint">💡 ${esc(err.hint)}</div>` : ""}
    <details><summary>Подробности ошибки</summary>
      <div class="grid">${rows.map(([k, v]) => `<span class="k">${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>
      ${err.body ? `<div class="k" style="margin-top:.4rem;color:var(--muted)">Ответ сервиса</div><pre>${esc(err.body)}</pre>` : ""}
      ${err.caused_by?.length ? `<div class="k" style="margin-top:.4rem;color:var(--muted)">Причина</div><pre>${esc(err.caused_by.join("\n"))}</pre>` : ""}
      ${err.traceback ? `<div class="k" style="margin-top:.4rem;color:var(--muted)">Трассировка</div><pre>${esc(err.traceback)}</pre>` : ""}
    </details>
    <div class="row"><button type="button" class="btn ghost sm" data-act="copy-error" data-p="${pid}">Копировать подробности</button></div>
  </div>`;
}

function cardHtml(g, pid, texts) {
  const t = g.transcripts?.[pid];
  const p = providerInfo(pid);
  const picked = g.provider === pid;
  const pinned = g.selected === pid;
  const archived = isArchived(pid); // read-only: it can be copied or pinned, never re-run
  if (!t) {
    return `<div class="tr-card"><div class="tr-head"><h3>${p.full}</h3><span class="badge">не отправлялся</span></div>
      <div class="row"><button type="button" class="btn secondary sm" data-act="send-one" data-p="${pid}">Распознать моделью ${p.name}</button></div></div>`;
  }
  if (t.status === "queued" || t.status === "running") {
    return `<div class="tr-card"><div class="tr-head"><h3>${p.full}</h3><span class="badge info"><span class="spinner" style="width:.55rem;height:.55rem"></span>${t.status === "queued" ? "в очереди" : "распознаю…"}</span></div></div>`;
  }
  if (t.status === "error") {
    return `<div class="tr-card error"><div class="tr-head"><h3>${p.full}</h3><span class="badge bad">ошибка</span></div>${errorHtml(t.error, pid)}
      ${archived ? "" : `<div class="row"><button type="button" class="btn secondary sm" data-act="send-one" data-p="${pid}">Повторить</button></div>`}</div>`;
  }
  const meta = [
    t.model ? `<span>${esc(t.model)}</span>` : "",
    t.elapsed_s ? `<span>${t.elapsed_s} с</span>` : "",
    `<span>звук: ${esc(processingLabel(t.processing_label, state.presets))}</span>`,
    t.finished_at ? `<span>${esc(fmtDate(t.finished_at))}</span>` : "",
    t.migrated ? `<span>из прежней версии</span>` : "",
  ].join("");
  const body = texts[pid] ?? esc(t.text);
  return `<div class="tr-card ${picked ? "picked" : ""}">
    <div class="tr-head"><h3>${p.full}</h3>${picked ? `<span class="badge ok" title="Этот текст идёт в «Текст записи»">в тексте записи</span>` : ""}${pinned ? `<span class="badge accent">закреплён</span>` : ""}</div>
    <div class="tr-meta">${meta}</div>
    <div class="tr-text">${t.text ? body : `<span class="muted">пустой ответ — модель не услышала речи</span>`}</div>
    <div class="row">
      <button type="button" class="btn ${pinned ? "ghost" : "secondary"} sm" data-act="pin" data-p="${pid}">${pinned ? "Открепить (выбирать автоматически)" : "Использовать этот текст"}</button>
      <button type="button" class="btn ghost sm" data-act="copy-text" data-p="${pid}">Копировать</button>
    </div></div>`;
}

function resendHtml(g) {
  const defaults = PROVIDER_IDS.filter((p) => !g.transcripts?.[p] || g.transcripts[p].status === "error");
  if (!resend.providers) resend.providers = defaults.length ? defaults : [...PROVIDER_IDS];
  // with a single model there is nothing to choose, so the chips only appear once there are several
  const chips = PROVIDER_IDS.length < 2 ? "" : PROVIDER_IDS.map(
    (p) => `<button type="button" class="chip ${resend.providers.includes(p) ? "on" : ""}" data-act="rs-prov" data-p="${p}">${PROVIDERS[p].name}</button>`
  ).join("");
  const options = Object.entries(state.presets.processing)
    .map(([k, v]) => `<option value="${k}" ${resend.preset === k && !resend.custom ? "selected" : ""}>${esc(v.label)}</option>`)
    .join("");
  const cur = processingLabel(g.processing_label, state.presets);
  const c = resend.params;
  return `<div class="resend">
    ${chips ? `<div class="row"><span class="mono muted" style="font-size:.74rem">Модели:</span>${chips}</div>` : ""}
    <div class="row"><span class="mono muted" style="font-size:.74rem">Звук:</span>
      <select data-act="rs-proc" style="flex:1">
        <option value="" ${!resend.preset && !resend.custom ? "selected" : ""}>как сейчас — ${esc(cur)}</option>${options}
        <option value="__custom__" ${resend.custom ? "selected" : ""}>Свои настройки…</option>
      </select></div>
    ${resend.custom ? `<div class="fields" style="grid-template-columns:repeat(2,1fr);gap:.5rem .8rem">
      <label class="check">срез, Гц <input type="number" data-rs="highpass_hz" min="0" max="400" step="10" value="${c.highpass_hz}"></label>
      <label class="check">шум, % <input type="number" data-rs="denoise" min="0" max="100" step="5" value="${Math.round(c.denoise * 100)}"></label>
      <label class="check">цель, дБ <input type="number" data-rs="target_dbfs" min="-40" max="-6" step="1" value="${c.target_dbfs}"></label>
      <label class="check">макс. усиление, дБ <input type="number" data-rs="max_gain_db" min="0" max="60" step="1" value="${c.max_gain_db}"></label>
      <label class="check"><input type="checkbox" data-rs="normalize" ${c.normalize ? "checked" : ""}> выровнять громкость</label></div>` : ""}
    <div class="row"><button type="button" class="btn primary" data-act="resend" ${resend.providers.length ? "" : "disabled"}>Распознать ещё раз</button>
      <span class="muted" style="font-size:.76rem">Прежние результаты останутся — их можно сравнить.</span></div>
  </div>`;
}

function dynamicHtml(s, g) {
  // A new transcript can be compared with the archived one it replaces.
  const giga = g.transcripts?.gigaam;
  const old = archivedIds(g).find((p) => g.transcripts[p].status === "done" && g.transcripts[p].text);
  const comparable = giga?.status === "done" && !!giga.text && !!old;
  let texts = {};
  if (comparable && diffOn) {
    const [a, b] = diffHtml(giga.text, g.transcripts[old].text);
    texts = { gigaam: a, [old]: b };
  }
  const single = g.cut_reason === "single" && g.duration_seconds > 60
    ? `<div class="notice">Это сессия целиком одним куском (${fmtClock(g.duration_seconds)}) — так записывала прежняя версия. Длинный кусок распознаётся хуже, а GigaAM принимает до 25 с за раз: нажмите «Пересобрать…» в заголовке сессии, чтобы получить короткие сегменты.</div>`
    : "";
  return `${single}
    <div class="insp-section"><h3>Звук</h3>${statsHtml(g)}</div>
    <div class="insp-section"><h3>Расшифровка</h3>
      ${comparable ? `<label class="check"><input type="checkbox" data-act="diff" ${diffOn ? "checked" : ""}> подсветить различия с прежней расшифровкой</label>` : ""}
      ${[...PROVIDER_IDS, ...archivedIds(g)].map((p) => cardHtml(g, p, texts)).join("")}</div>
    <div class="insp-section"><h3>Распознать ещё раз</h3>${resendHtml(g)}</div>`;
}

function neighbours(s, g) {
  const i = s.segments.findIndex((x) => x.id === g.id);
  return { prev: s.segments[i - 1] || null, next: s.segments[i + 1] || null };
}

export function renderInspector() {
  const g = selectedSegment();
  const s = g ? session(state.selected.sid) : null;
  shell.classList.toggle("with-inspector", !!g);
  root.classList.toggle("hidden", !g);
  if (!g || !s) {
    shownKey = null;
    root.innerHTML = "";
    return;
  }
  const key = `${s.id}/${g.id}`;
  if (shownKey !== key) {
    shownKey = key;
    audioStamp = Date.now();
    resend.providers = null;
    const { prev, next } = neighbours(s, g);
    root.innerHTML = `<div class="insp">
      <div class="insp-head"><h2>Сегмент ${esc(g.id)} · сессия ${esc(s.id)}</h2>
        <button type="button" class="btn ghost sm" data-act="prev" ${prev ? "" : "disabled"} title="Предыдущий (←)">◀</button>
        <button type="button" class="btn ghost sm" data-act="next" ${next ? "" : "disabled"} title="Следующий (→)">▶</button>
        <button type="button" class="btn ghost sm" data-act="close" title="Закрыть (Esc)">✕</button></div>
      <div class="insp-sub"><span>${fmtClock(g.start_s)}–${fmtClock(g.end_s)}</span><span>${(g.duration_seconds || 0).toFixed(1)} с</span>
        <span title="Почему здесь разрез">${esc(CUT_REASONS[g.cut_reason] || g.cut_reason)}</span>
        ${g.gap_before_s > 0.05 ? `<span title="Тишина, не вошедшая в сегменты">пауза перед: ${g.gap_before_s.toFixed(1)} с</span>` : ""}</div>
      <div class="ab">
        <div class="box"><label>Оригинал</label><audio controls preload="none" src="${url(s.id, g.id, "raw")}"></audio></div>
        <div class="box"><label>После обработки</label><audio controls preload="none" data-kind="processed" src="${url(s.id, g.id, "processed")}"></audio></div>
      </div>
      <div id="inspDyn"></div></div>`;
    root.scrollTop = 0;
  }
  $("inspDyn").innerHTML = dynamicHtml(s, g);
}

let lastParams = "";
on("selection", () => {
  const g = selectedSegment();
  lastParams = JSON.stringify(g?.audio_params || null);
  renderInspector();
  if (state.selected?.provider) {
    root.querySelector(`[data-p="${state.selected.provider}"]`)?.closest(".tr-card")?.scrollIntoView({ block: "nearest" });
  }
});

on("segment", ({ sid, segment: g }) => {
  if (state.selected?.sid !== sid || state.selected?.gid !== g.id) return;
  const params = JSON.stringify(g.audio_params || null);
  if (params !== lastParams) {
    // the processed audio was regenerated: make the player fetch it again
    lastParams = params;
    audioStamp = Date.now();
    const player = root.querySelector('audio[data-kind="processed"]');
    if (player) {
      const wasPaused = player.paused;
      player.src = url(sid, g.id, "processed");
      if (!wasPaused) player.pause();
    }
  }
  renderInspector();
});

on("providers", () => state.selected && renderInspector());
on("sessions", () => {
  if (state.selected && !selectedSegment()) close();
});
on("recording-opened", () => {
  if (state.selected && !selectedSegment()) close();
});

function close() {
  state.selected = null;
  document.querySelectorAll(".seg-row.selected").forEach((r) => r.classList.remove("selected"));
  emit("selection");
}

function step(delta) {
  const g = selectedSegment();
  const s = g && session(state.selected.sid);
  if (!g || !s) return;
  const next = s.segments[s.segments.findIndex((x) => x.id === g.id) + delta];
  if (next) selectSegment(s.id, next.id, { scroll: true });
}

root.addEventListener("click", (event) => {
  const el = event.target.closest("[data-act]");
  if (!el) return;
  const g = selectedSegment();
  const sid = state.selected?.sid;
  if (!g) return;
  const act = el.dataset.act;
  const pid = el.dataset.p;
  if (act === "close") return close();
  if (act === "prev") return step(-1);
  if (act === "next") return step(1);
  if (act === "send-one") return void guard(() => sendSegment(sid, g.id, [pid]));
  if (act === "pin") {
    return void guard(async () => {
      await api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/segments/${enc(g.id)}`, { method: "PATCH", body: { selected: g.selected === pid ? null : pid } });
    });
  }
  if (act === "copy-text") {
    return void copyText(g.transcripts?.[pid]?.text || "").then((ok) => setStatus(ok ? "Текст скопирован" : "Не удалось скопировать", ok ? "ok" : "error", null, 2000));
  }
  if (act === "copy-error") {
    const err = g.transcripts?.[pid]?.error;
    return void copyText(JSON.stringify({ segment: `${sid}/${g.id}`, ...err }, null, 2)).then(() => setStatus("Подробности ошибки скопированы", "ok", null, 2000));
  }
  if (act === "rs-prov") {
    resend.providers = resend.providers.includes(pid) ? resend.providers.filter((p) => p !== pid) : [...resend.providers, pid];
    return renderInspector();
  }
  if (act === "resend") {
    return void guard(async () => {
      const body = { providers: resend.providers };
      if (resend.custom) body.processing = resend.params;
      else if (resend.preset) body.preset = resend.preset;
      const res = await api(`/recordings/${enc(state.recId)}/sessions/${enc(sid)}/segments/${enc(g.id)}/transcribe`, { method: "POST", body });
      setStatus(res.queued.length ? "Отправлено" : "Уже выполняется", res.queued.length ? "ok" : "", null, 2000);
    });
  }
});

root.addEventListener("change", (event) => {
  const el = event.target;
  if (el.dataset.act === "diff") {
    diffOn = el.checked;
    return renderInspector();
  }
  if (el.dataset.act === "rs-proc") {
    resend.custom = el.value === "__custom__";
    resend.preset = resend.custom ? "" : el.value;
    return renderInspector();
  }
  if (el.dataset.rs) {
    const key = el.dataset.rs;
    if (el.type === "checkbox") resend.params[key] = el.checked;
    else resend.params[key] = key === "denoise" ? Number(el.value) / 100 : Number(el.value);
  }
});

window.addEventListener("keydown", (event) => {
  const tag = event.target.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || event.ctrlKey || event.metaKey) return;
  if (!$("modal").classList.contains("hidden") || !$("drawer").classList.contains("hidden")) return;
  if (event.key === "Escape" && state.selected) close();
  else if (event.key === "ArrowLeft" && state.selected) step(-1);
  else if (event.key === "ArrowRight" && state.selected) step(1);
});
