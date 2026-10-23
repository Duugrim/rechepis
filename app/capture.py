"""Live capture: one WebSocket = one session.

The browser sends 16-bit mono PCM at the device's own sample rate. Here it is converted to 16 kHz with a
proper polyphase filter, written to disk at once (so a crash loses nothing), and fed to the segmenter.
Every segment the segmenter releases is registered, its level statistics are recorded, and — if the
user chose targets — sent off for transcription while the recording goes on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from . import store
from .audio import SAMPLE_RATE
from .audio.resample import StreamResampler
from .audio.segmenter import Cut, Segmenter, single_cut
from .audio.wavio import read_pcm
from .config import load_config
from .events import EventHub
from .jobs import TranscriptionManager
from .settings import PROVIDER_IDS, normalize_mic, normalize_processing, normalize_slicing, normalize_targets

log = logging.getLogger("rechepis.capture")

INFO_EVERY_S = 0.25
MIN_SESSION_S = 0.5

_active: "LiveSession | None" = None
_background: set[asyncio.Task[None]] = set()


class LiveSession:
    def __init__(
        self,
        *,
        recording_id: str,
        device_rate: int,
        slicing: dict[str, Any],
        processing: dict[str, Any],
        targets: list[str],
        language: str,
        mic: dict[str, Any],
        manager: TranscriptionManager,
        hub: EventHub,
    ) -> None:
        self.recording_id = recording_id
        self.slicing = normalize_slicing(slicing)
        self.processing = normalize_processing(processing)
        self.targets = normalize_targets(targets)
        self.manager = manager
        self.hub = hub
        meta = store.create_session(
            recording_id,
            slicing=self.slicing,
            processing=self.processing,
            targets=self.targets,
            language=language,
            device_rate=device_rate,
            mic=mic,
        )
        self.session_id: str = meta["id"]
        self.meta = meta
        self.dir: Path = store.session_dir(recording_id, self.session_id)
        self._pcm_path = self.dir / "audio.pcm"
        self._pcm = open(self._pcm_path, "wb")
        self._resampler = StreamResampler(device_rate, SAMPLE_RATE)
        self._segmenter = Segmenter(self.slicing) if self.slicing["enabled"] else None
        self.samples = 0
        self.segments = 0
        self._queue: asyncio.Queue[Cut | None] = asyncio.Queue()
        self._worker = asyncio.create_task(self._run_worker(), name=f"session-{self.session_id}")
        self._finished = False
        self.started = time.monotonic()
        hub.publish({"type": "session", "recording_id": recording_id, "session": store.get_session(recording_id, self.session_id)})

    # -- incoming audio ------------------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if self._finished or len(data) < 2:
            return
        pcm = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2").astype(np.float32) / 32768.0
        self._push(self._resampler.process(pcm))

    def _push(self, audio: np.ndarray) -> None:
        if len(audio) == 0:
            return
        self._pcm.write((np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
        self.samples += len(audio)
        if self._segmenter is not None:
            cuts = self._segmenter.feed(audio)
            if cuts:
                self._pcm.flush()  # the worker reads these samples back from the file
                for cut in cuts:
                    self._queue.put_nowait(cut)

    def info(self) -> dict[str, Any]:
        seg = self._segmenter
        return {
            "type": "level",
            "elapsed": round(self.samples / SAMPLE_RATE, 2),
            "segments": self.segments,
            "db": round(seg.level_db, 1) if seg else None,
            "floor": round(seg.noise_floor_db, 1) if seg else None,
            "threshold": round(seg.threshold_db, 1) if seg else None,
            "dip": round(seg.dip_threshold_db, 1) if seg else None,
        }

    # -- finishing -----------------------------------------------------------------------------------

    async def finish(self) -> None:
        """Flush the tail, wait for every segment to be registered, finalise the audio file."""
        if self._finished:
            return
        self._finished = True
        self._push(self._resampler.process(np.zeros(0, dtype=np.float32), final=True))
        self._pcm.flush()
        if self._segmenter is not None:
            for cut in self._segmenter.flush():
                self._queue.put_nowait(cut)
        elif self.samples > MIN_SESSION_S * SAMPLE_RATE:
            for cut in single_cut(self.samples):
                self._queue.put_nowait(cut)
        self._pcm.close()
        self._queue.put_nowait(None)
        await self._worker
        duration = self.samples / SAMPLE_RATE
        if duration < MIN_SESSION_S:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.hub.publish({"type": "session_removed", "recording_id": self.recording_id, "session_id": self.session_id})
            return
        await asyncio.to_thread(store.finalize_session_audio, self.recording_id, self.session_id)
        floor = self._segmenter.noise_floor_db if self._segmenter else None
        await asyncio.to_thread(
            store.update_session,
            self.recording_id,
            self.session_id,
            status="ready",
            ended_at=store.utc_now(),
            duration_seconds=round(duration, 3),
            noise_floor_db=round(floor, 1) if floor is not None else None,
        )
        await asyncio.to_thread(store.rebuild_transcripts, self.recording_id, self.session_id)
        self.hub.publish(
            {"type": "session", "recording_id": self.recording_id, "session": store.get_session(self.recording_id, self.session_id)}
        )

    # -- segment registration ------------------------------------------------------------------------

    async def _run_worker(self) -> None:
        while True:
            cut = await self._queue.get()
            if cut is None:
                return
            try:
                await asyncio.to_thread(self._register, cut)
            except Exception:  # noqa: BLE001
                log.exception("could not register segment %s", cut)

    def _register(self, cut: Cut) -> None:
        raw = read_pcm(self._pcm_path, cut.start, cut.end - cut.start)
        meta = store.add_segment(self.recording_id, self.session_id, cut.to_dict(), processing=self.processing, raw=raw)
        self.segments += 1
        segment = store.segment_public(self.recording_id, self.session_id, meta["id"])
        self.hub.publish(
            {"type": "segment", "recording_id": self.recording_id, "session_id": self.session_id, "segment": segment}
        )
        for provider in self.targets:
            self.manager.submit(self.recording_id, self.session_id, meta["id"], provider)


async def capture_endpoint(websocket: WebSocket, manager: TranscriptionManager, hub: EventHub) -> None:
    global _active
    await websocket.accept()
    session: LiveSession | None = None
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            try:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass

    last_info = 0.0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if session is not None:
                    session.feed(message["bytes"])
                    now = time.monotonic()
                    if now - last_info >= INFO_EVERY_S:
                        last_info = now
                        await send(session.info())
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                await send({"type": "error", "message": "Некорректное управляющее сообщение"})
                continue
            action = payload.get("action")
            if action == "start":
                if session is not None:
                    await send({"type": "error", "message": "Сессия уже идёт"})
                    continue
                if _active is not None:
                    await send({"type": "error", "message": "Запись уже идёт в другой вкладке"})
                    continue
                try:
                    session = _open_session(payload, manager, hub)
                except FileNotFoundError:
                    await send({"type": "error", "message": "Запись не найдена"})
                    continue
                except (ValueError, KeyError) as exc:
                    await send({"type": "error", "message": f"Не удалось начать сессию: {exc}"})
                    continue
                _active = session
                await send({"type": "started", "session": store.read_session_meta(session.recording_id, session.session_id)})
            elif action == "stop":
                break
            elif action == "ping":
                await send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("capture socket failed")
        await send({"type": "error", "message": f"Ошибка сервера: {exc}"})
    finally:
        if session is not None:
            if _active is session:
                _active = None
            # The tail must be saved even if the browser is already gone, so this is not tied to the socket.
            task = asyncio.create_task(session.finish(), name=f"finish-{session.session_id}")
            _background.add(task)
            task.add_done_callback(_background.discard)
            try:
                await asyncio.shield(task)
                await send({"type": "session_done", "session": store.get_session(session.recording_id, session.session_id)})
            except Exception as exc:  # noqa: BLE001
                log.exception("finishing the session failed")
                await send({"type": "error", "message": f"Сессия сохранена не полностью: {exc}"})
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


def _open_session(payload: dict[str, Any], manager: TranscriptionManager, hub: EventHub) -> LiveSession:
    cfg = load_config()
    recording_id = str(payload.get("recording_id") or "").strip()
    if not recording_id:
        raise ValueError("не указана запись")
    store.recording_dir(recording_id)  # FileNotFoundError / ValueError if wrong
    targets = payload["targets"] if "targets" in payload else cfg["targets"]
    return LiveSession(
        recording_id=recording_id,
        device_rate=max(8000, min(192000, int(payload.get("sample_rate") or SAMPLE_RATE))),
        slicing={**cfg["slicing"], **(payload.get("slicing") or {})},
        processing={**cfg["processing"], **(payload.get("processing") or {})},
        targets=[t for t in normalize_targets(targets) if t in PROVIDER_IDS],
        language=str(payload.get("language") or cfg["language"]),
        mic=normalize_mic({**cfg["mic"], **(payload.get("mic") or {})}),
        manager=manager,
        hub=hub,
    )


async def drain_background() -> None:
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)
