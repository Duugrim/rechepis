import { emit, state, upsertSegment, upsertSession } from "./state.js";

let socket = null;
let retry = 0;
let everConnected = false;

/** Server push: segments appearing, transcripts finishing, model download progress. Reconnects by itself. */
export function connectEvents() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/events`);
  socket.onopen = () => {
    retry = 0;
    if (everConnected) emit("reconnected");
    everConnected = true;
    emit("connection", true);
  };
  socket.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handle(msg);
  };
  socket.onclose = () => {
    emit("connection", false);
    retry += 1;
    setTimeout(connectEvents, Math.min(8000, 500 * retry));
  };
  socket.onerror = () => socket.close();
}

function handle(msg) {
  const mine = msg.recording_id && msg.recording_id === state.recId;
  switch (msg.type) {
    case "providers":
      state.providers = msg.providers;
      state.queues = msg.queues || {};
      emit("providers");
      break;
    case "segment":
      if (mine) {
        upsertSegment(msg.session_id, msg.segment);
        emit("segment", { sid: msg.session_id, segment: msg.segment });
      }
      emit("library-dirty");
      break;
    case "session":
      if (mine) {
        upsertSession(msg.session);
        emit("session", msg.session);
      }
      emit("library-dirty");
      break;
    case "session_removed":
      if (mine && state.rec) {
        state.rec.sessions = state.rec.sessions.filter((s) => s.id !== msg.session_id);
        emit("sessions");
      }
      emit("library-dirty");
      break;
    case "transcript":
      if (mine && state.rec) {
        state.rec.transcript = msg.transcript;
        emit("transcript");
      }
      emit("library-dirty");
      break;
    default:
      break;
  }
}
