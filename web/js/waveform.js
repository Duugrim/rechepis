import { fmtClock } from "./util.js";

const COLORS = {
  idle: "154,175,156",
  working: "106,168,201",
  done: "111,191,138",
  error: "210,101,79",
  preview: "94,195,176",
};

function niceStep(duration, width) {
  const target = Math.max(1, Math.floor(width / 90));
  const raw = duration / target;
  for (const s of [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]) if (s >= raw) return s;
  return 3600;
}

/**
 * Draws a session: the waveform, the stretches that became segments (coloured by how far their
 * transcription got) and the selected one. With `regions` it can also show a proposed cut.
 */
export function drawWave(canvas, { peaks, duration, segments = [], selectedGid = null, regions = null, hoverT = null }) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(10, Math.floor(canvas.clientWidth * dpr));
  const h = Math.max(10, Math.floor(canvas.clientHeight * dpr));
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, w, h);
  const total = duration || Math.max(1, ...segments.map((s) => s.end_s), ...(regions || []).map((r) => r.end_s));
  const x = (t) => (t / total) * w;
  const axis = Math.round(14 * dpr);
  const mid = (h - axis) / 2;
  const amp = mid - 3 * dpr;

  const list = regions
    ? regions.map((r) => ({ start_s: r.start_s, end_s: r.end_s, state: "preview", id: null }))
    : segments.map((s) => ({ start_s: s.start_s, end_s: s.end_s, state: s.state || "idle", id: s.id }));

  for (const r of list) {
    const selected = r.id !== null && r.id === selectedGid;
    ctx.fillStyle = `rgba(${COLORS[r.state] || COLORS.idle}, ${selected ? 0.34 : 0.16})`;
    ctx.fillRect(x(r.start_s), 0, Math.max(1, x(r.end_s) - x(r.start_s)), h - axis);
    ctx.fillStyle = `rgba(${COLORS[r.state] || COLORS.idle}, 0.9)`;
    ctx.fillRect(x(r.start_s), 0, Math.max(1, dpr), h - axis);
    if (selected) {
      ctx.strokeStyle = "rgba(214,164,90,1)";
      ctx.lineWidth = 2 * dpr;
      ctx.strokeRect(x(r.start_s) + dpr, dpr, Math.max(2, x(r.end_s) - x(r.start_s) - 2 * dpr), h - axis - 2 * dpr);
    }
  }

  if (peaks?.length) {
    const wave = (alpha, clip) => {
      ctx.save();
      if (clip) {
        ctx.beginPath();
        for (const r of list) ctx.rect(x(r.start_s), 0, Math.max(1, x(r.end_s) - x(r.start_s)), h);
        ctx.clip();
      }
      ctx.strokeStyle = `rgba(232,240,230,${alpha})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let px = 0; px < w; px++) {
        const a = Math.floor((px / w) * peaks.length);
        const b = Math.max(a + 1, Math.floor(((px + 1) / w) * peaks.length));
        let lo = 0;
        let hi = 0;
        for (let i = a; i < b && i < peaks.length; i++) {
          lo = Math.min(lo, peaks[i][0]);
          hi = Math.max(hi, peaks[i][1]);
        }
        ctx.moveTo(px + 0.5, mid - hi * amp - 0.5);
        ctx.lineTo(px + 0.5, mid - lo * amp + 0.5);
      }
      ctx.stroke();
      ctx.restore();
    };
    wave(0.22, false);
    wave(0.75, true);
  }

  // time axis
  ctx.fillStyle = "rgba(154,175,156,0.9)";
  ctx.font = `${Math.round(10 * dpr)}px "IBM Plex Mono", monospace`;
  ctx.textBaseline = "bottom";
  const step = niceStep(total, w / dpr);
  for (let t = 0; t < total; t += step) {
    ctx.fillRect(x(t), h - axis, dpr, 4 * dpr);
    ctx.fillText(fmtClock(t), x(t) + 3 * dpr, h - 1);
  }

  if (hoverT !== null) {
    ctx.fillStyle = "rgba(232,240,230,0.5)";
    ctx.fillRect(x(hoverT), 0, dpr, h - axis);
  }
}

/** Wires a canvas to its data: redraws on resize, reports hover/click as a time and the segment under it. */
export function attachWave(canvas, tip, getData, { onPick }) {
  let hoverT = null;
  const redraw = () => drawWave(canvas, { ...getData(), hoverT });
  const timeAt = (ev) => {
    const rect = canvas.getBoundingClientRect();
    const { duration, segments } = getData();
    const total = duration || Math.max(1, ...segments.map((s) => s.end_s));
    return { t: Math.max(0, Math.min(total, ((ev.clientX - rect.left) / rect.width) * total)), px: ev.clientX - rect.left };
  };
  const segmentAt = (t) => {
    const { segments } = getData();
    return segments.find((s) => t >= s.start_s && t <= s.end_s) || null;
  };
  canvas.addEventListener("mousemove", (ev) => {
    const { t, px } = timeAt(ev);
    hoverT = t;
    const seg = segmentAt(t);
    tip.textContent = seg ? `${seg.id} · ${fmtClock(seg.start_s)}–${fmtClock(seg.end_s)}` : fmtClock(t);
    tip.style.left = `${px}px`;
    tip.classList.remove("hidden");
    redraw();
  });
  canvas.addEventListener("mouseleave", () => {
    hoverT = null;
    tip.classList.add("hidden");
    redraw();
  });
  canvas.addEventListener("click", (ev) => {
    const seg = segmentAt(timeAt(ev).t);
    if (seg) onPick(seg, timeAt(ev).t);
  });
  new ResizeObserver(redraw).observe(canvas);
  return { redraw };
}
