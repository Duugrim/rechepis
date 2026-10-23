import { emit, state } from "./state.js";

/**
 * Microphone -> server. One call to `start` is one session.
 *
 * The page does no signal processing at all: the microphone is opened with the browser's own noise
 * suppression / echo cancellation / auto gain switched off (they are what made old recordings pump and
 * gate), the samples are sent at the device's native rate, and the server resamples, cuts and cleans.
 */

let ctx = null;
let stream = null;
let node = null;
let mute = null;
let ws = null;
let wakeLock = null;
let started = false;
let pending = [];
let finishing = null;
let startedAt = 0;
let timerId = null;

export const isRecording = () => !!state.live;

function close(reason) {
  if (timerId) clearInterval(timerId);
  timerId = null;
  if (node) {
    node.port.onmessage = null;
    try { node.disconnect(); } catch { /* already gone */ }
  }
  if (mute) {
    try { mute.disconnect(); } catch { /* already gone */ }
  }
  node = mute = null;
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
  ctx?.close().catch(() => {});
  ctx = null;
  wakeLock?.release?.().catch?.(() => {});
  wakeLock = null;
  started = false;
  pending = [];
  state.live = null;
  emit("live", { active: false, reason });
}

export async function listMics(askPermission = false) {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  let devices = await navigator.mediaDevices.enumerateDevices();
  if (askPermission && devices.every((d) => d.kind !== "audioinput" || !d.label)) {
    const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
    probe.getTracks().forEach((t) => t.stop());
    devices = await navigator.mediaDevices.enumerateDevices();
  }
  return devices
    .filter((d) => d.kind === "audioinput" && d.deviceId !== "communications")
    .map((d, i) => ({ id: d.deviceId, label: d.label || `Микрофон ${i + 1}` }));
}

/**
 * @param {{recordingId: string, targets: string[], slicing: object, processing: object, mic: object, language: string}} opts
 * @returns {Promise<void>} resolves once the server has accepted the session
 */
export async function startSession(opts) {
  if (state.live) throw new Error("Сессия уже идёт");
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error("Браузер не даёт доступ к микрофону на этом адресе. Откройте http://127.0.0.1:8765");
  }
  const constraints = {
    audio: {
      channelCount: 1,
      echoCancellation: !!opts.mic.echo_cancellation,
      noiseSuppression: !!opts.mic.noise_suppression,
      autoGainControl: !!opts.mic.auto_gain,
      ...(opts.mic.device_id ? { deviceId: { exact: opts.mic.device_id } } : {}),
    },
  };
  try {
    stream = await navigator.mediaDevices.getUserMedia(constraints);
  } catch (err) {
    if (err.name === "OverconstrainedError" && opts.mic.device_id) {
      delete constraints.audio.deviceId;
      stream = await navigator.mediaDevices.getUserMedia(constraints);
    } else {
      throw new Error(err.name === "NotAllowedError" ? "Доступ к микрофону запрещён в браузере" : `Микрофон недоступен: ${err.message || err}`);
    }
  }

  try {
    ctx = new AudioContext({ latencyHint: "interactive" });
    await ctx.audioWorklet.addModule("/static/js/recorder-worklet.js");
    if (ctx.state === "suspended") await ctx.resume();
    const source = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, "pcm-capture", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);
  } catch (err) {
    close("error");
    throw new Error(`Не удалось запустить захват звука: ${err.message || err}`);
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/capture`);
  ws.binaryType = "arraybuffer";
  started = false;
  pending = [];
  state.live = { recordingId: opts.recordingId, sessionId: null, level: -100, peak: 0, elapsed: 0, segments: 0 };

  const accepted = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Сервер не ответил на начало сессии")), 8000);
    ws.onopen = () =>
      ws.send(
        JSON.stringify({
          action: "start",
          recording_id: opts.recordingId,
          sample_rate: ctx.sampleRate,
          targets: opts.targets,
          slicing: opts.slicing,
          processing: opts.processing,
          language: opts.language,
          mic: opts.mic,
        })
      );
    ws.onerror = () => {
      clearTimeout(timeout);
      reject(new Error("Нет связи с сервером для записи"));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "started") {
        clearTimeout(timeout);
        started = true;
        state.live.sessionId = msg.session.id;
        for (const chunk of pending) ws.send(chunk);
        pending = [];
        resolve(msg.session);
      } else if (msg.type === "error") {
        clearTimeout(timeout);
        if (!started) reject(new Error(msg.message));
        else emit("live-error", msg.message);
      } else if (msg.type === "level") {
        Object.assign(state.live, { server: msg, elapsed: msg.elapsed, segments: msg.segments });
        emit("live-info", msg);
      } else if (msg.type === "session_done") {
        finishing?.(msg.session);
      }
    };
    ws.onclose = () => {
      if (state.live && !finishing) {
        emit("live-error", "Связь с сервером потеряна. Сохранённая часть сессии останется в записи.");
        close("lost");
      }
    };
  });

  node.port.onmessage = (ev) => {
    const { pcm, peak, rms } = ev.data;
    if (state.live) {
      state.live.level = rms;
      state.live.peak = peak;
    }
    if (state.live) (state.live.peaks ||= []).push([-peak, peak]);
    emit("live-level", { rms, peak });
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (started) ws.send(pcm);
    else if (pending.length < 400) pending.push(pcm); // ~17 s of audio waiting for the "started" answer
  };

  let accepted_session;
  try {
    accepted_session = await accepted;
  } catch (err) {
    try { ws.close(); } catch { /* ignore */ }
    ws = null;
    close("error");
    throw err;
  }

  startedAt = performance.now();
  timerId = setInterval(() => emit("live-tick", (performance.now() - startedAt) / 1000), 250);
  try {
    wakeLock = await navigator.wakeLock?.request("screen");
  } catch {
    wakeLock = null; // best effort: a sleeping screen is fine, a sleeping computer is not
  }
  emit("live", { active: true, sessionId: state.live.sessionId });
  return accepted_session;
}

/** Ends the session; resolves with the finished session once the server has saved the tail. */
export async function stopSession() {
  if (!state.live || !ws) return null;
  const socket = ws;
  const done = new Promise((resolve) => {
    finishing = resolve;
    setTimeout(() => resolve(null), 120000);
  });
  node?.port && (node.port.onmessage = null);
  try {
    socket.send(JSON.stringify({ action: "stop" }));
  } catch { /* the socket is gone; the server finishes on its own */ }
  stream?.getTracks().forEach((t) => t.stop());
  emit("live", { active: false, saving: true });
  const result = await done;
  finishing = null;
  try { socket.close(); } catch { /* already closed */ }
  ws = null;
  close("stopped");
  return result;
}
