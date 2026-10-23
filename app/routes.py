from __future__ import annotations

import asyncio
import logging
import os
import statistics
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import store
from .audio import SAMPLE_RATE
from .audio.segmenter import cut_audio, single_cut
from .audio.wavio import peaks as wav_peaks
from .config import load_config, public_config, save_config
from .events import hub
from .jobs import TranscriptionManager
from .providers import get_provider, get_providers
from .providers.gigaam import GigaAMProvider
from .settings import (
    GIGAAM_MODELS,
    PROCESSING_PRESETS,
    SLICING_PRESETS,
    normalize_processing,
    normalize_slicing,
    normalize_targets,
    processing_from_preset,
    processing_label,
    slicing_from_preset,
    slicing_label,
)

log = logging.getLogger("rechepis.routes")
router = APIRouter(prefix="/api")

manager = TranscriptionManager(hub)
_busy_sessions: set[tuple[str, str]] = set()
_peaks_cache: dict[tuple[str, float, int, int], list[list[float]]] = {}


# -- helpers -----------------------------------------------------------------------------------------


def _not_found(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, "Не найдено")
    return HTTPException(400, str(exc))


def _provider_statuses() -> dict[str, Any]:
    return {pid: p.status() for pid, p in get_providers().items()}


def _resolve_processing(preset: str | None, params: dict[str, Any] | None) -> dict[str, Any] | None:
    """``None`` means "whatever the segment already has"."""
    if preset:
        try:
            return processing_from_preset(preset)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    if params:
        return normalize_processing(params)
    return None


def _check_providers_ready(provider_ids: list[str]) -> list[str]:
    ids = normalize_targets(provider_ids)
    if not ids:
        raise HTTPException(400, "Не выбрана модель распознавания")
    for pid in ids:
        status = get_provider(pid).status()
        if not status["ready"]:
            raise HTTPException(400, f"{status['label']}: {status['message']}")
    return ids


def publish_providers() -> None:
    hub.publish({"type": "providers", "providers": _provider_statuses(), "queues": manager.queue_sizes()})


# -- config and providers ----------------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@router.get("/config")
def get_config() -> dict[str, Any]:
    return {
        "config": public_config(),
        "providers": _provider_statuses(),
        "queues": manager.queue_sizes(),
        "presets": {
            "processing": {k: {"label": v[0], "params": processing_from_preset(k)} for k, v in PROCESSING_PRESETS.items()},
            "slicing": {k: {"label": v[0], "params": slicing_from_preset(k)} for k, v in SLICING_PRESETS.items()},
        },
        "gigaam_models": GIGAAM_MODELS,
    }


class ConfigUpdate(BaseModel):
    language: str | None = None
    gigaam: dict[str, Any] | None = None
    targets: list[str] | None = None
    paragraph_gap_s: float | None = None
    slicing: dict[str, Any] | None = None
    slicing_preset: str | None = None
    processing: dict[str, Any] | None = None
    processing_preset: str | None = None
    mic: dict[str, Any] | None = None


@router.put("/config")
def put_config(body: ConfigUpdate) -> dict[str, Any]:
    save_config(body.model_dump(exclude_none=True))
    publish_providers()
    return get_config()


@router.get("/providers")
def providers_status() -> dict[str, Any]:
    return {"providers": _provider_statuses(), "queues": manager.queue_sizes()}


class DownloadBody(BaseModel):
    model: str


@router.post("/providers/gigaam/download")
def gigaam_download(body: DownloadBody) -> dict[str, Any]:
    provider = get_provider("gigaam")
    assert isinstance(provider, GigaAMProvider)
    if body.model not in GIGAAM_MODELS:
        raise HTTPException(400, "Неизвестная модель")
    if not provider.status()["installed"]:
        raise HTTPException(400, "Пакеты GigaAM не установлены: запустите Install GigaAM.bat")
    started = provider.start_download(body.model)
    return {"started": started, "provider": provider.status()}


@router.post("/providers/gigaam/warm")
def gigaam_warm() -> dict[str, Any]:
    provider = get_provider("gigaam")
    assert isinstance(provider, GigaAMProvider)
    if not provider.status()["ready"]:
        raise HTTPException(400, provider.status()["message"])
    provider.warm_up()
    return {"ok": True}


# -- recordings --------------------------------------------------------------------------------------


@router.get("/recordings")
def list_recordings() -> dict[str, Any]:
    return {"recordings": store.list_recordings()}


class CreateRecording(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@router.post("/recordings")
def create_recording(body: CreateRecording) -> dict[str, Any]:
    return store.create_recording(body.title, language=load_config()["language"])


@router.get("/recordings/{rid}")
def get_recording(rid: str) -> dict[str, Any]:
    try:
        return store.get_recording(rid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@router.patch("/recordings/{rid}")
def rename_recording(rid: str, body: RenameBody) -> dict[str, Any]:
    try:
        return store.rename_recording(rid, body.title)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None


@router.post("/recordings/{rid}/open")
def open_recording(rid: str) -> dict[str, Any]:
    try:
        path = store.recording_dir(rid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    os.startfile(str(path))  # type: ignore[attr-defined]
    return {"ok": True, "path": str(path)}


@router.post("/recordings/{rid}/sessions/{sid}/open")
def open_session(rid: str, sid: str) -> dict[str, Any]:
    try:
        path = store.session_dir(rid, sid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    os.startfile(str(path))  # type: ignore[attr-defined]
    return {"ok": True, "path": str(path)}


# -- sessions ----------------------------------------------------------------------------------------


@router.get("/recordings/{rid}/sessions/{sid}/peaks")
async def session_peaks(rid: str, sid: str, buckets: int = Query(1200, ge=50, le=6000)) -> dict[str, Any]:
    try:
        path, kind = store.session_audio_source(rid, sid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    if kind != "wav":
        return {"peaks": [], "duration": 0.0}
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size, buckets)
    if key not in _peaks_cache:
        if len(_peaks_cache) > 64:
            _peaks_cache.clear()
        _peaks_cache[key] = await asyncio.to_thread(wav_peaks, path, buckets)
    meta = store.read_session_meta(rid, sid)
    return {"peaks": _peaks_cache[key], "duration": meta.get("duration_seconds") or 0.0}


@router.get("/recordings/{rid}/sessions/{sid}/audio")
def session_audio(rid: str, sid: str) -> FileResponse:
    try:
        path, kind = store.session_audio_source(rid, sid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    if kind != "wav":
        raise HTTPException(409, "Сессия ещё записывается")
    return FileResponse(path, media_type="audio/wav")


class ResegmentBody(BaseModel):
    slicing: dict[str, Any] | None = None
    slicing_preset: str | None = None
    processing: dict[str, Any] | None = None
    processing_preset: str | None = None
    targets: list[str] | None = None
    dry_run: bool = False


def _cuts_for(rid: str, sid: str, slicing: dict[str, Any]) -> tuple[list[Any], np.ndarray]:
    audio = store.read_session_audio(rid, sid)
    if slicing["enabled"]:
        return cut_audio(audio, slicing), audio
    return single_cut(len(audio)), audio


def _cut_summary(cuts: list[Any], total_s: float) -> dict[str, Any]:
    durations = [c.duration_s for c in cuts]
    kept = sum(durations)
    return {
        "count": len(cuts),
        "kept_s": round(kept, 1),
        "dropped_s": round(max(0.0, total_s - kept), 1),
        "min_s": round(min(durations), 1) if durations else 0,
        "median_s": round(statistics.median(durations), 1) if durations else 0,
        "max_s": round(max(durations), 1) if durations else 0,
        "forced": sum(1 for c in cuts if c.forced),
    }


@router.post("/recordings/{rid}/sessions/{sid}/resegment")
async def resegment(rid: str, sid: str, body: ResegmentBody) -> dict[str, Any]:
    try:
        session = store.read_session_meta(rid, sid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    if session.get("status") != "ready":
        raise HTTPException(409, "Сессия ещё записывается или обрабатывается")
    if body.slicing_preset:
        slicing = slicing_from_preset(body.slicing_preset)
    else:
        slicing = normalize_slicing({**session["slicing"], **(body.slicing or {})})
    processing = _resolve_processing(body.processing_preset, body.processing) or normalize_processing(session["processing"])

    cuts, audio = await asyncio.to_thread(_cuts_for, rid, sid, slicing)
    total_s = len(audio) / SAMPLE_RATE
    summary = _cut_summary(cuts, total_s)
    if body.dry_run:
        return {
            "summary": summary,
            "segments": [{"start_s": c.start / SAMPLE_RATE, "end_s": c.end / SAMPLE_RATE, "reason": c.reason} for c in cuts],
            "slicing": slicing,
        }
    targets = normalize_targets(body.targets if body.targets is not None else [])
    if targets:
        _check_providers_ready(targets)
    key = (rid, sid)
    if key in _busy_sessions:
        raise HTTPException(409, "Эта сессия уже пересобирается")
    _busy_sessions.add(key)
    await asyncio.to_thread(store.update_session, rid, sid, status="processing")
    hub.publish({"type": "session", "recording_id": rid, "session": await asyncio.to_thread(store.get_session, rid, sid)})
    task = asyncio.create_task(_apply_cuts(rid, sid, cuts, audio, slicing, processing, targets))
    task.add_done_callback(lambda _t: None)
    return {"ok": True, "summary": summary}


async def _apply_cuts(
    rid: str, sid: str, cuts: list[Any], audio: np.ndarray, slicing: dict[str, Any], processing: dict[str, Any], targets: list[str]
) -> None:
    def work() -> None:
        store.archive_segments(rid, sid)
        store.update_session(
            rid, sid, slicing=slicing, slicing_label=slicing_label(slicing),
            processing=processing, processing_label=processing_label(processing), targets=targets,
        )
        for i, cut in enumerate(cuts):
            raw = audio[cut.start : cut.end]
            meta = store.add_segment(rid, sid, cut.to_dict(), processing=processing, raw=raw, index=i)
            hub.publish(
                {"type": "segment", "recording_id": rid, "session_id": sid, "segment": store.segment_public(rid, sid, meta["id"])}
            )
            for pid in targets:
                manager.submit(rid, sid, meta["id"], pid)

    try:
        await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        log.exception("re-cut failed")
        await asyncio.to_thread(store.update_session, rid, sid, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await asyncio.to_thread(store.update_session, rid, sid, status="ready")
            combined = await asyncio.to_thread(store.rebuild_transcripts, rid, sid)
            hub.publish({"type": "transcript", "recording_id": rid, "transcript": combined})
            hub.publish({"type": "session", "recording_id": rid, "session": await asyncio.to_thread(store.get_session, rid, sid)})
        finally:
            _busy_sessions.discard((rid, sid))


class TranscribeBody(BaseModel):
    providers: list[str]
    processing: dict[str, Any] | None = None
    preset: str | None = None
    only: Literal["all", "missing", "errors"] = "all"


@router.post("/recordings/{rid}/sessions/{sid}/transcribe")
async def transcribe_session(rid: str, sid: str, body: TranscribeBody) -> dict[str, Any]:
    providers = _check_providers_ready(body.providers)
    processing = _resolve_processing(body.preset, body.processing)
    try:
        session = await asyncio.to_thread(store.get_session, rid, sid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    queued = 0
    for seg in session["segments"]:
        for pid in providers:
            entry = (seg.get("transcripts") or {}).get(pid) or {}
            status = entry.get("status")
            if status in ("queued", "running"):
                continue
            if body.only == "missing" and status == "done":
                continue
            if body.only == "errors" and status != "error":
                continue
            if await asyncio.to_thread(manager.submit, rid, sid, seg["id"], pid, processing):
                queued += 1
    return {"queued": queued}


# -- segments ----------------------------------------------------------------------------------------


@router.get("/recordings/{rid}/sessions/{sid}/segments/{gid}/audio")
async def segment_audio(rid: str, sid: str, gid: str, kind: Literal["processed", "raw"] = "processed") -> FileResponse:
    try:
        if kind == "raw":
            path = await asyncio.to_thread(store.raw_segment_wav, rid, sid, gid)
        else:
            path = await asyncio.to_thread(store.prepare_segment_audio, rid, sid, gid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    return FileResponse(path, media_type="audio/wav")


@router.post("/recordings/{rid}/sessions/{sid}/segments/{gid}/transcribe")
async def transcribe_segment(rid: str, sid: str, gid: str, body: TranscribeBody) -> dict[str, Any]:
    providers = _check_providers_ready(body.providers)
    processing = _resolve_processing(body.preset, body.processing)
    try:
        await asyncio.to_thread(store.read_segment_meta, rid, sid, gid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    queued = [pid for pid in providers if await asyncio.to_thread(manager.submit, rid, sid, gid, pid, processing)]
    return {"queued": queued}


class SegmentPatch(BaseModel):
    selected: str | None = Field(default=None, description="transcript that stands for the segment; null = automatic")


@router.patch("/recordings/{rid}/sessions/{sid}/segments/{gid}")
async def patch_segment(rid: str, sid: str, gid: str, body: SegmentPatch) -> dict[str, Any]:
    try:
        await asyncio.to_thread(store.set_selected, rid, sid, gid, body.selected)
        segment = await asyncio.to_thread(store.segment_public, rid, sid, gid)
        combined = await asyncio.to_thread(store.rebuild_transcripts, rid, sid)
    except (FileNotFoundError, ValueError) as exc:
        raise _not_found(exc) from None
    hub.publish({"type": "segment", "recording_id": rid, "session_id": sid, "segment": segment})
    hub.publish({"type": "transcript", "recording_id": rid, "transcript": combined})
    return {"segment": segment, "transcript": combined}


# -- websockets --------------------------------------------------------------------------------------


async def events_socket(websocket: WebSocket) -> None:
    import json

    await websocket.accept()
    queue = hub.subscribe()
    try:
        await websocket.send_text(
            json.dumps({"type": "providers", "providers": _provider_statuses(), "queues": manager.queue_sizes()}, ensure_ascii=False)
        )
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — a closed tab is the normal way out
        pass
    finally:
        hub.unsubscribe(queue)
