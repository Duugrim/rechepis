export const $ = (id) => document.getElementById(id);

export function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const pad2 = (n) => String(n).padStart(2, "0");

/** 75 -> "1:15", 3725 -> "1:02:05" */
export function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}

/** Human duration: "45 с", "3 мин 12 с", "1 ч 04 мин". */
export function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return `${sec} с`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h) return `${h} ч ${pad2(m)} мин`;
  return `${m} мин ${pad2(sec % 60)} с`;
}

export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function fmtDb(v) {
  return v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}`;
}

export const debounce = (fn, ms) => {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
};

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(`rechepis.${key}`);
    if (value === null) localStorage.removeItem(`rechepis.${key}`);
    else localStorage.setItem(`rechepis.${key}`, value);
  } catch {
    // private mode or blocked storage: the app just forgets
  }
  return null;
}

/* ---------- toast and overlay ---------- */

let statusTimer = null;
export function setStatus(text, kind = "", action = null, ms = 0) {
  const box = document.getElementById("status");
  clearTimeout(statusTimer);
  box.className = `status ${kind}`;
  box.textContent = text;
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn secondary sm";
    button.textContent = action.label;
    button.addEventListener("click", action.onClick);
    box.appendChild(button);
  }
  statusTimer = setTimeout(() => box.classList.add("hidden"), ms || (kind === "error" ? 9000 : 5000));
}

export function showOverlay(text, progress = null) {
  $("overlay").classList.remove("hidden");
  $("overlayText").textContent = text;
  $("overlayBar").style.width = progress === null ? "100%" : `${Math.round(progress * 100)}%`;
}
export const hideOverlay = () => $("overlay").classList.add("hidden");

export async function guard(fn) {
  try {
    return await fn();
  } catch (error) {
    hideOverlay();
    setStatus(error.message || String(error), "error");
    return undefined;
  }
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/* ---------- vocabulary ---------- */

export const PROVIDERS = {
  gigaam: { short: "Г", name: "GigaAM", full: "GigaAM (локально)" },
};
export const PROVIDER_IDS = ["gigaam"];

// Transcripts of a provider this version no longer ships (those of version 1.x). They stay readable and can
// be picked as the text of a segment, but nothing new is ever sent to them.
export const ARCHIVE = { short: "А", name: "Архив", full: "Прежняя расшифровка (архив)" };
export const providerInfo = (id) => PROVIDERS[id] || ARCHIVE;
export const isArchived = (id) => !PROVIDER_IDS.includes(id);
export const archivedIds = (g) => Object.keys(g?.transcripts || {}).filter(isArchived);

// Where new segments go. Stored as a list of provider ids; "none" = only record.
export const TARGET_CHOICES = [
  { id: "none", label: "Не отправлять", list: [] },
  { id: "gigaam", label: "GigaAM", list: ["gigaam"] },
];
export const targetChoiceId = (list) =>
  TARGET_CHOICES.find((t) => t.list.length === list.length && t.list.every((p) => list.includes(p)))?.id || "none";

export const CUT_REASONS = {
  long_pause: "долгая пауза",
  pause: "пауза после целевой длины",
  max_len: "достигнут максимум, разрез по паузе",
  forced: "принудительно: пауз не было",
  end: "конец сессии",
  single: "сессия целиком",
};

export const WARNINGS = {
  quiet: { short: "тихо", text: "Речь очень тихая: усиление помогает, но шум растёт вместе с ней. Поднесите микрофон ближе." },
  clipping: { short: "перегруз", text: "Звук упирается в максимум (клиппинг): уменьшите усиление микрофона в системе." },
  noisy: { short: "шумно", text: "Речь лишь ненамного громче фона — распознавание может ошибаться." },
  no_speech: { short: "нет речи", text: "В этом отрезке не найдено речи." },
};

export const STAGES = {
  config: "настройки",
  audio: "подготовка звука",
  model: "загрузка модели",
  transcribe: "распознавание",
  interrupted: "прервано",
};

export function processingLabel(name, presets) {
  if (!name) return "—";
  if (name === "custom") return "свои настройки";
  return presets?.processing?.[name]?.label || name;
}

/** Word level diff of two texts: returns [htmlA, htmlB] with <del>/<ins> around the words that differ. */
export function diffHtml(a, b) {
  const tokenize = (t) => t.split(/(\s+)/).filter((x) => x.length);
  const norm = (w) => w.toLowerCase().replace(/ё/g, "е").replace(/[^\p{L}\p{N}]+/gu, "");
  const ta = tokenize(a);
  const tb = tokenize(b);
  const wa = ta.map((w, i) => [i, norm(w)]).filter(([, n]) => n);
  const wb = tb.map((w, i) => [i, norm(w)]).filter(([, n]) => n);
  if (wa.length * wb.length > 4_000_000) return [esc(a), esc(b)];
  const n = wa.length;
  const m = wb.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = wa[i][1] === wb[j][1] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const sameA = new Set();
  const sameB = new Set();
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (wa[i][1] === wb[j][1]) {
      sameA.add(wa[i][0]);
      sameB.add(wb[j][0]);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  const render = (tokens, same, tag) =>
    tokens.map((w, idx) => (!norm(w) || same.has(idx) ? esc(w) : `<${tag}>${esc(w)}</${tag}>`)).join("");
  return [render(ta, sameA, "del"), render(tb, sameB, "ins")];
}
