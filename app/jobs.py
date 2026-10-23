"""Background transcription of segments.

A job is "send this segment to that provider with this processing". Each provider has its own queue and
its own number of parallel workers (``CONCURRENCY``); the local model runs one job at a time. Jobs live in
memory only; their state (queued / running / done / error) is written into the segment meta, so the UI and
a restart see the same truth.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from . import store
from .events import EventHub
from .providers import ProviderError, describe_error, get_provider
from .settings import PROVIDER_IDS, normalize_processing, processing_label

log = logging.getLogger("rechepis.jobs")

CONCURRENCY = {"gigaam": 1}


@dataclass(frozen=True)
class Job:
    recording_id: str
    session_id: str
    segment_id: str
    provider: str
    processing: dict[str, Any] | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.recording_id, self.session_id, self.segment_id, self.provider)


class TranscriptionManager:
    def __init__(self, hub: EventHub) -> None:
        self._hub = hub
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[str, asyncio.Queue[Job]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._pending: set[tuple[str, str, str, str]] = set()
        self._running: dict[str, int] = {p: 0 for p in PROVIDER_IDS}

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        for provider in PROVIDER_IDS:
            self._queues[provider] = asyncio.Queue()
            for _ in range(CONCURRENCY.get(provider, 1)):
                self._tasks.append(asyncio.create_task(self._worker(provider), name=f"transcribe-{provider}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def queue_sizes(self) -> dict[str, dict[str, int]]:
        return {p: {"queued": q.qsize(), "running": self._running[p]} for p, q in self._queues.items()}

    # -- submitting ----------------------------------------------------------------------------------

    def submit(
        self,
        recording_id: str,
        session_id: str,
        segment_id: str,
        provider: str,
        processing: dict[str, Any] | None = None,
    ) -> bool:
        """Queue a segment. Thread-safe. Returns False if the same job is already waiting or running."""
        if provider not in PROVIDER_IDS:
            raise ValueError(f"Неизвестный провайдер: {provider}")
        job = Job(recording_id, session_id, segment_id, provider, normalize_processing(processing) if processing else None)
        if job.key in self._pending:
            return False
        self._pending.add(job.key)
        try:
            store.set_transcript(
                recording_id,
                session_id,
                segment_id,
                provider,
                {"status": "queued", "queued_at": store.utc_now(), "error": None},
                rebuild=False,
            )
        except Exception:
            self._pending.discard(job.key)
            raise
        self._publish_segment(job)
        loop = self._loop
        if loop is None:
            raise RuntimeError("Менеджер расшифровки не запущен")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._queues[provider].put_nowait(job)
        else:
            loop.call_soon_threadsafe(self._queues[provider].put_nowait, job)
        return True

    # -- workers -------------------------------------------------------------------------------------

    async def _worker(self, provider: str) -> None:
        queue = self._queues[provider]
        while True:
            job = await queue.get()
            self._running[provider] += 1
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("job crashed: %s", job)
            finally:
                self._running[provider] -= 1
                self._pending.discard(job.key)
                queue.task_done()

    def _publish_segment(self, job: Job) -> None:
        try:
            segment = store.segment_public(job.recording_id, job.session_id, job.segment_id)
        except Exception:  # noqa: BLE001
            return
        self._hub.publish(
            {"type": "segment", "recording_id": job.recording_id, "session_id": job.session_id, "segment": segment}
        )

    async def _run(self, job: Job) -> None:
        rid, sid, gid, provider_id = job.key
        context: dict[str, Any] = {"segment": f"{sid}/{gid}"}
        started_at = store.utc_now()
        try:
            await asyncio.to_thread(
                store.set_transcript, rid, sid, gid, provider_id,
                {"status": "running", "started_at": started_at, "error": None}, None, False,
            )
            self._publish_segment(job)
            provider = get_provider(provider_id)
            meta = await asyncio.to_thread(store.read_segment_meta, rid, sid, gid)
            params = normalize_processing(job.processing if job.processing is not None else meta["processing"])
            session = await asyncio.to_thread(store.read_session_meta, rid, sid)
            language = session.get("language") or "ru"
            try:
                path = await asyncio.to_thread(store.prepare_segment_audio, rid, sid, gid, params)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"Не удалось подготовить звук: {type(exc).__name__}: {exc}", stage="audio") from exc
            context.update(
                provider=provider_id,
                audio_s=meta.get("duration_seconds"),
                audio_bytes=path.stat().st_size,
                processing=processing_label(params),
                language=language,
            )
            result = await provider.transcribe(path, language=language, duration_s=float(meta.get("duration_seconds") or 0))
            entry = {
                "status": "done",
                "text": result.text.strip(),
                "model": result.model,
                "elapsed_s": round(result.elapsed_s, 2),
                "finished_at": store.utc_now(),
                "processing": params,
                "processing_label": processing_label(params),
                "error": None,
                "migrated": False,
            }
            doc = {
                "text": result.text,
                "model": result.model,
                "elapsed_s": result.elapsed_s,
                "segments": result.segments,
                "raw": result.raw,
                "processing": params,
            }
            await asyncio.to_thread(store.set_transcript, rid, sid, gid, provider_id, entry, doc, False)
            self._pending.discard(job.key)  # the result is on disk: a new request for this segment is legitimate
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("transcription failed %s: %s", job.key, exc)
            stage = getattr(exc, "stage", "transcribe")
            info = describe_error(exc, provider=provider_id, stage=stage, context=context)
            entry = {"status": "error", "text": "", "finished_at": store.utc_now(), "error": info}
            try:
                await asyncio.to_thread(store.set_transcript, rid, sid, gid, provider_id, entry, None, False)
            except Exception:  # noqa: BLE001
                log.exception("could not record the error of %s", job.key)
            self._pending.discard(job.key)
        finally:
            await self._finish(job)

    async def _finish(self, job: Job) -> None:
        try:
            combined = await asyncio.to_thread(store.rebuild_transcripts, job.recording_id, job.session_id)
        except Exception:  # noqa: BLE001
            log.exception("transcript rebuild failed")
            return
        self._publish_segment(job)
        self._hub.publish({"type": "transcript", "recording_id": job.recording_id, "transcript": combined})
