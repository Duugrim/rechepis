/** Shared state and a tiny event bus. Modules read `state`, change it through the helpers, and `emit` what changed. */

export const state = {
  config: null, // public config from the server
  presets: { processing: {}, slicing: {} },
  gigaModels: {},
  providers: {}, // id -> status
  queues: {},
  recordings: [], // library list
  recId: null,
  rec: null, // full recording: {id, meta, transcript, sessions: [{..., segments: []}]}
  selected: null, // {sid, gid, provider?}
  view: "auto", // which model's text the transcript shows
  open: {}, // session id -> expanded?
  sendBar: {}, // session id -> state of the "send all" strip
  peaks: {}, // `${rid}/${sid}` -> {peaks, duration}
  live: null, // {recordingId, sessionId, ...} while a capture runs
  micDevices: [],
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) {
    try {
      fn(payload);
    } catch (error) {
      console.error(`handler of "${event}" failed`, error);
    }
  }
}

export const session = (sid) => state.rec?.sessions.find((s) => s.id === sid) || null;
export const segment = (sid, gid) => session(sid)?.segments.find((g) => g.id === gid) || null;

export function selectedSegment() {
  return state.selected ? segment(state.selected.sid, state.selected.gid) : null;
}

export function upsertSegment(sid, seg) {
  const s = session(sid);
  if (!s) return;
  const i = s.segments.findIndex((g) => g.id === seg.id);
  if (i >= 0) s.segments[i] = seg;
  else s.segments.push(seg);
}

export function upsertSession(data) {
  if (!state.rec) return;
  const i = state.rec.sessions.findIndex((s) => s.id === data.id);
  if (i >= 0) {
    const segments = data.segments ?? state.rec.sessions[i].segments;
    state.rec.sessions[i] = { ...state.rec.sessions[i], ...data, segments };
  } else {
    state.rec.sessions.push({ segments: [], ...data });
    state.rec.sessions.sort((a, b) => Number(a.id) - Number(b.id));
  }
}

export const providerReady = (id) => !!state.providers[id]?.ready;

/** The packages of the local model are there, but the chosen model has not been downloaded yet. */
export const modelMissing = () => !!state.providers.gigaam?.installed && !state.providers.gigaam?.downloaded;
