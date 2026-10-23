"""On-disk model: recording -> sessions -> segments.

::

    recordings/<id>/
      meta.json                    title, language, timestamps
      transcript.txt               all sessions joined (the text the user copies)
      sessions/<NNN>/
        meta.json                  one press of "start": times, slicing/processing used, counters
        audio.wav                  the raw capture, 16 kHz mono (audio.pcm while still recording)
        transcript.txt
        segments/<NNN>/
          meta.json                where it lies in the session, audio stats, per-provider transcripts
          audio.wav                what was actually sent: the slice after processing
          <provider>.json          the provider's full answer
          transcript.txt           the text currently shown for this segment

Every change goes through this module, under a per-recording lock and with atomic file replacement, so
a crash can never leave a half-written meta file behind.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .audio import SAMPLE_RATE
from .audio.pipeline import analyze, process_audio, warnings_for
from .audio.wavio import pcm_to_wav, read_pcm, read_wav, wav_frames, write_wav
from .config import RECORDINGS_DIR, ensure_dirs, load_config
from .settings import (
    PROVIDER_IDS,
    normalize_processing,
    normalize_slicing,
    processing_from_preset,
    processing_label,
    slicing_label,
    slicing_from_preset,
)
from .textfmt import join_segment_texts

SCHEMA_VERSION = 2
NOISE_CONTEXT_S = 90  # how much earlier audio is used to learn what the room sounds like
# Version 1.x sent every recording to a cloud service and stored its answer under this key. The service is no
# longer supported, but those texts are still the only text of many old segments, so they stay readable.
V1_PROVIDER_ID = "palatine"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- files and locks ---------------------------------------------------------------------------------


def _replace(tmp: Path, path: Path) -> None:
    """Atomic replacement. On Windows it fails for a moment while a reader has the target open."""
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    for attempt in range(10):  # the mirror image of _replace: opening a file that is being swapped can fail
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable")


def write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _replace(tmp, path)


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def rec_lock(recording_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(recording_id, threading.RLock())


_seg_locks: dict[tuple[str, str, str], threading.Lock] = {}


def _segment_lock(rid: str, sid: str, gid: str) -> threading.Lock:
    with _locks_guard:
        return _seg_locks.setdefault((rid, sid, gid), threading.Lock())


def _slugify(title: str) -> str:
    text = title.strip().lower()
    text = re.sub(r"[^\w\s\-а-яё]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:48] or "recording"


def _numeric_ids(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted((p.name for p in directory.iterdir() if p.is_dir() and p.name.isdigit()), key=int)


def _next_id(directory: Path) -> str:
    ids = _numeric_ids(directory)
    return f"{(int(ids[-1]) + 1) if ids else 1:03d}"


def _check_name(name: str, what: str) -> str:
    if not name or name != Path(name).name or name in (".", ".."):
        raise ValueError(f"Invalid {what} id")
    return name


# -- paths -------------------------------------------------------------------------------------------

_migrated: set[str] = set()


def recording_dir(recording_id: str) -> Path:
    ensure_dirs()
    safe = _check_name(recording_id, "recording")
    path = (RECORDINGS_DIR / safe).resolve()
    if RECORDINGS_DIR.resolve() not in path.parents:
        raise ValueError("Invalid recording id")
    if not path.is_dir():
        raise FileNotFoundError(recording_id)
    if safe not in _migrated:
        migrate_recording(path)
        _migrated.add(safe)
    return path


def session_dir(recording_id: str, session_id: str) -> Path:
    path = recording_dir(recording_id) / "sessions" / _check_name(session_id, "session")
    if not path.is_dir():
        raise FileNotFoundError(f"{recording_id}/{session_id}")
    return path


def segment_dir(recording_id: str, session_id: str, segment_id: str) -> Path:
    path = session_dir(recording_id, session_id) / "segments" / _check_name(segment_id, "segment")
    if not path.is_dir():
        raise FileNotFoundError(f"{recording_id}/{session_id}/{segment_id}")
    return path


# -- recordings --------------------------------------------------------------------------------------


def create_recording(title: str, language: str = "ru") -> dict[str, Any]:
    ensure_dirs()
    title = (title or "").strip() or "Без названия"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = f"{stamp}_{_slugify(title)}"
    path = RECORDINGS_DIR / base
    suffix = 1
    while path.exists():
        path = RECORDINGS_DIR / f"{base}_{suffix}"
        suffix += 1
    (path / "sessions").mkdir(parents=True)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "id": path.name,
        "title": title,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "language": language,
    }
    write_json(path / "meta.json", meta)
    write_text(path / "transcript.txt", "")
    _migrated.add(path.name)
    return {"id": path.name, "path": str(path), "meta": meta}


def read_recording_meta(recording_id: str) -> dict[str, Any]:
    path = recording_dir(recording_id) / "meta.json"
    return read_json(path) if path.exists() else {"id": recording_id}


def _touch_recording(recording_id: str, **fields: Any) -> dict[str, Any]:
    with rec_lock(recording_id):
        meta = read_recording_meta(recording_id)
        meta.update(fields)
        meta["updated_at"] = utc_now()
        write_json(recording_dir(recording_id) / "meta.json", meta)
        return meta


def rename_recording(recording_id: str, title: str) -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise ValueError("Название не может быть пустым")
    return _touch_recording(recording_id, title=title)


def list_recordings() -> list[dict[str, Any]]:
    ensure_dirs()
    items: list[dict[str, Any]] = []
    for entry in sorted(RECORDINGS_DIR.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "meta.json").exists():
            continue
        try:
            recording_dir(entry.name)
            meta = read_json(entry / "meta.json")
        except (json.JSONDecodeError, OSError, FileNotFoundError, ValueError):
            continue
        sessions = 0
        segments = 0
        duration = 0.0
        working = errors = 0
        for sid in _numeric_ids(entry / "sessions"):
            try:
                s = read_json(entry / "sessions" / sid / "meta.json")
            except (json.JSONDecodeError, OSError):
                continue
            sessions += 1
            segments += int(s.get("segment_count") or 0)
            duration += float(s.get("duration_seconds") or 0)
            counts = s.get("counts") or {}
            working += int(counts.get("working") or 0)
            errors += int(counts.get("error") or 0)
        transcript = ""
        tpath = entry / "transcript.txt"
        if tpath.exists():
            try:
                transcript = tpath.read_text(encoding="utf-8")[:300]
            except OSError:
                pass
        items.append(
            {
                "id": entry.name,
                "title": meta.get("title") or entry.name,
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "session_count": sessions,
                "segment_count": segments,
                "duration_seconds": round(duration, 1),
                "transcript_preview": transcript.strip(),
                "working": working,
                "errors": errors,
            }
        )
    return items


def get_recording(recording_id: str) -> dict[str, Any]:
    path = recording_dir(recording_id)
    meta = read_recording_meta(recording_id)
    transcript = (path / "transcript.txt").read_text(encoding="utf-8") if (path / "transcript.txt").exists() else ""
    sessions = [get_session(recording_id, sid) for sid in _numeric_ids(path / "sessions")]
    return {
        "id": recording_id,
        "path": str(path),
        "meta": meta,
        "transcript": transcript,
        "sessions": sessions,
    }


# -- sessions ----------------------------------------------------------------------------------------


def create_session(
    recording_id: str,
    *,
    slicing: dict[str, Any],
    processing: dict[str, Any],
    targets: list[str],
    language: str,
    device_rate: int | None = None,
    mic: dict[str, Any] | None = None,
    source: str = "live",
) -> dict[str, Any]:
    with rec_lock(recording_id):
        sessions = recording_dir(recording_id) / "sessions"
        sessions.mkdir(exist_ok=True)
        sid = _next_id(sessions)
        path = sessions / sid
        (path / "segments").mkdir(parents=True)
        slicing = normalize_slicing(slicing)
        processing = normalize_processing(processing)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "id": sid,
            "recording_id": recording_id,
            "source": source,
            "started_at": utc_now(),
            "ended_at": None,
            "status": "recording",
            "sample_rate": SAMPLE_RATE,
            "device_sample_rate": device_rate,
            "duration_seconds": 0.0,
            "language": language,
            "slicing": slicing,
            "slicing_label": slicing_label(slicing),
            "processing": processing,
            "processing_label": processing_label(processing),
            "targets": list(targets),
            "mic": mic or {},
            "segment_count": 0,
            "kept_seconds": 0.0,
            "counts": {},
            "noise_floor_db": None,
            "error": None,
        }
        write_json(path / "meta.json", meta)
        write_text(path / "transcript.txt", "")
        _touch_recording(recording_id)
        return meta


def read_session_meta(recording_id: str, session_id: str) -> dict[str, Any]:
    return read_json(session_dir(recording_id, session_id) / "meta.json")


def update_session(recording_id: str, session_id: str, **fields: Any) -> dict[str, Any]:
    with rec_lock(recording_id):
        path = session_dir(recording_id, session_id) / "meta.json"
        meta = read_json(path)
        meta.update(fields)
        write_json(path, meta)
        return meta


def get_session(recording_id: str, session_id: str) -> dict[str, Any]:
    path = session_dir(recording_id, session_id)
    meta = read_json(path / "meta.json")
    segments = []
    for gid in _numeric_ids(path / "segments"):
        try:
            segments.append(segment_public(recording_id, session_id, gid))
        except (OSError, json.JSONDecodeError):
            continue
    audio = path / "audio.wav"
    return {
        **meta,
        "has_audio": audio.exists(),
        "live": (path / "audio.pcm").exists() and not audio.exists(),
        "segments": segments,
    }


def session_audio_source(recording_id: str, session_id: str) -> tuple[Path, str]:
    """The best available raw audio of a session: the wav, or the pcm that is still being written."""
    path = session_dir(recording_id, session_id)
    if (path / "audio.wav").exists():
        return path / "audio.wav", "wav"
    if (path / "audio.pcm").exists():
        return path / "audio.pcm", "pcm"
    raise FileNotFoundError("У сессии нет аудио")


def read_session_audio(recording_id: str, session_id: str, start: int = 0, frames: int = -1) -> np.ndarray:
    """Raw samples (16 kHz) ``[start, start+frames)`` of a session."""
    path, kind = session_audio_source(recording_id, session_id)
    if kind == "pcm":
        total = path.stat().st_size // 2
        count = total - start if frames < 0 else min(frames, total - start)
        return read_pcm(path, start, max(0, count))
    data, rate = read_wav(path, start=start, frames=frames)
    if rate != SAMPLE_RATE:
        from .audio.resample import resample

        data = resample(data, rate, SAMPLE_RATE)
    return data


def session_samples(recording_id: str, session_id: str) -> int:
    path, kind = session_audio_source(recording_id, session_id)
    if kind == "pcm":
        return path.stat().st_size // 2
    frames, rate = wav_frames(path)
    return int(round(frames * SAMPLE_RATE / rate)) if rate != SAMPLE_RATE else frames


def finalize_session_audio(recording_id: str, session_id: str) -> Path:
    """Turn the raw pcm of a finished capture into audio.wav."""
    path = session_dir(recording_id, session_id)
    pcm = path / "audio.pcm"
    wav = path / "audio.wav"
    if pcm.exists():
        pcm_to_wav(pcm, wav, SAMPLE_RATE)
        pcm.unlink(missing_ok=True)
    return wav


# -- segments ----------------------------------------------------------------------------------------


def effective_provider(seg: dict[str, Any]) -> str | None:
    """Whose text stands for the segment: the user's pick, else a supported provider's, else an archived one.

    "Archived" are transcripts of providers this version no longer ships (see ``V1_PROVIDER_ID``). They are
    never produced any more, but they are not dropped either: an old recording keeps its text.
    """
    transcripts = seg.get("transcripts") or {}

    def done(p: str) -> bool:
        entry = transcripts.get(p)
        return bool(entry and entry.get("status") == "done")

    selected = seg.get("selected")
    if isinstance(selected, str) and done(selected):
        return selected
    for p in (*PROVIDER_IDS, *transcripts):
        if done(p):
            return p
    return None


def segment_state(seg: dict[str, Any]) -> str:
    entries = list((seg.get("transcripts") or {}).values())
    if any(e.get("status") in ("queued", "running") for e in entries):
        return "working"
    if any(e.get("status") == "done" for e in entries):
        return "done"
    if any(e.get("status") == "error" for e in entries):
        return "error"
    return "idle"


def segment_public(recording_id: str, session_id: str, segment_id: str) -> dict[str, Any]:
    path = session_dir(recording_id, session_id) / "segments" / segment_id
    meta = read_json(path / "meta.json")
    provider = effective_provider(meta)
    text = meta["transcripts"][provider]["text"] if provider else ""
    return {
        **meta,
        "state": segment_state(meta),
        "provider": provider,
        "text": text,
        "has_audio": (path / "audio.wav").exists(),
    }


def read_segment_meta(recording_id: str, session_id: str, segment_id: str) -> dict[str, Any]:
    return read_json(segment_dir(recording_id, session_id, segment_id) / "meta.json")


def update_segment(
    recording_id: str, session_id: str, segment_id: str, change: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Read-modify-write of one segment's meta under the recording lock."""
    with rec_lock(recording_id):
        path = segment_dir(recording_id, session_id, segment_id) / "meta.json"
        meta = read_json(path)
        change(meta)
        write_json(path, meta)
        return meta


def add_segment(
    recording_id: str,
    session_id: str,
    cut: dict[str, Any],
    *,
    processing: dict[str, Any],
    raw: np.ndarray | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Register a freshly cut segment. ``raw`` (its samples) lets us record level statistics at once."""
    processing = normalize_processing(processing)
    stats = analyze(raw) if raw is not None and len(raw) else None
    with rec_lock(recording_id):
        segments = session_dir(recording_id, session_id) / "segments"
        gid = _next_id(segments)
        path = segments / gid
        path.mkdir(parents=True)
        start_s = cut["start"] / SAMPLE_RATE
        end_s = cut["end"] / SAMPLE_RATE
        meta = {
            "id": gid,
            "session_id": session_id,
            "recording_id": recording_id,
            "index": int(gid) - 1 if index is None else index,
            "start_s": round(start_s, 3),
            "end_s": round(end_s, 3),
            "duration_seconds": round(end_s - start_s, 3),
            "speech_s": cut.get("speech_s"),
            "gap_before_s": cut.get("gap_before_s", 0.0),
            "cut_reason": cut.get("reason", "pause"),
            "created_at": utc_now(),
            "processing": processing,
            "processing_label": processing_label(processing),
            "audio_params": None,
            "audio": {"original": stats, "processed": None, "steps": [], "warnings": warnings_for(stats) if stats else []},
            "transcripts": {},
            "selected": None,
        }
        write_json(path / "meta.json", meta)
        write_text(path / "transcript.txt", "")
        smeta_path = segments.parent / "meta.json"
        smeta = read_json(smeta_path)
        smeta["segment_count"] = len(_numeric_ids(segments))
        smeta["kept_seconds"] = round(float(smeta.get("kept_seconds") or 0) + (end_s - start_s), 3)
        write_json(smeta_path, smeta)
    return meta


def segment_audio_path(recording_id: str, session_id: str, segment_id: str) -> Path:
    return segment_dir(recording_id, session_id, segment_id) / "audio.wav"


def raw_segment_samples(recording_id: str, session_id: str, meta: dict[str, Any]) -> np.ndarray:
    start = int(round(meta["start_s"] * SAMPLE_RATE))
    count = int(round(meta["end_s"] * SAMPLE_RATE)) - start
    return read_session_audio(recording_id, session_id, start, count)


def prepare_segment_audio(
    recording_id: str,
    session_id: str,
    segment_id: str,
    processing: dict[str, Any] | None = None,
    force: bool = False,
) -> Path:
    """Make ``audio.wav`` of a segment with the given (or its stored) processing. Blocking and CPU heavy."""
    with _segment_lock(recording_id, session_id, segment_id):
        meta = read_segment_meta(recording_id, session_id, segment_id)
        params = normalize_processing(processing if processing is not None else meta["processing"])
        path = segment_dir(recording_id, session_id, segment_id) / "audio.wav"
        if path.exists() and meta.get("audio_params") == params and not force:
            return path
        raw = raw_segment_samples(recording_id, session_id, meta)
        context = None
        if params["denoise"] > 0:
            ctx_start = max(0, int(meta["start_s"] * SAMPLE_RATE) - NOISE_CONTEXT_S * SAMPLE_RATE)
            ctx_len = int(round(meta["end_s"] * SAMPLE_RATE)) - ctx_start
            context = read_session_audio(recording_id, session_id, ctx_start, ctx_len)
        processed, report = process_audio(raw, params, context=context)
        write_wav(path, processed)

        def change(m: dict[str, Any]) -> None:
            m["processing"] = params
            m["processing_label"] = processing_label(params)
            m["audio_params"] = params
            m["audio"] = {
                "original": report["original"],
                "processed": report.get("processed"),
                "steps": report["steps"],
                "warnings": report["warnings"],
            }

        update_segment(recording_id, session_id, segment_id, change)
        return path


def raw_segment_wav(recording_id: str, session_id: str, segment_id: str) -> Path:
    """The unprocessed slice as a wav file (cached) for listening and for sending without processing."""
    with _segment_lock(recording_id, session_id, segment_id):
        meta = read_segment_meta(recording_id, session_id, segment_id)
        path = segment_dir(recording_id, session_id, segment_id) / "raw.wav"
        if not path.exists():
            write_wav(path, raw_segment_samples(recording_id, session_id, meta))
        return path


def set_selected(recording_id: str, session_id: str, segment_id: str, provider: str | None) -> dict[str, Any]:
    def change(m: dict[str, Any]) -> None:
        if provider is not None and provider not in PROVIDER_IDS and provider not in (m.get("transcripts") or {}):
            raise ValueError("Неизвестный провайдер")
        m["selected"] = provider

    meta = update_segment(recording_id, session_id, segment_id, change)
    rebuild_transcripts(recording_id, session_id)
    return meta


def set_transcript(
    recording_id: str,
    session_id: str,
    segment_id: str,
    provider: str,
    entry: dict[str, Any],
    result_doc: dict[str, Any] | None = None,
    rebuild: bool = True,
) -> dict[str, Any]:
    """Store one provider's entry (status/text/error) for a segment."""

    def change(m: dict[str, Any]) -> None:
        current = dict((m.get("transcripts") or {}).get(provider) or {})
        current.update(entry)
        m.setdefault("transcripts", {})[provider] = current

    meta = update_segment(recording_id, session_id, segment_id, change)
    if result_doc is not None:
        write_json(segment_dir(recording_id, session_id, segment_id) / f"{provider}.json", result_doc)
    if rebuild:
        rebuild_transcripts(recording_id, session_id)
    return meta


def rebuild_transcripts(recording_id: str, session_id: str | None = None) -> str:
    """Re-derive transcript.txt of the segments, the sessions and the recording from the stored entries."""
    cfg = load_config()
    gap = cfg["paragraph_gap_s"]
    with rec_lock(recording_id):
        rpath = recording_dir(recording_id)
        session_texts: list[str] = []
        for sid in _numeric_ids(rpath / "sessions"):
            spath = rpath / "sessions" / sid
            if session_id is not None and sid != session_id and (spath / "transcript.txt").exists():
                session_texts.append((spath / "transcript.txt").read_text(encoding="utf-8").strip())
                continue
            items: list[tuple[str, float]] = []
            counts = {"working": 0, "error": 0, "done": 0, "idle": 0}
            for gid in _numeric_ids(spath / "segments"):
                try:
                    meta = read_json(spath / "segments" / gid / "meta.json")
                except (OSError, json.JSONDecodeError):
                    continue
                provider = effective_provider(meta)
                text = meta["transcripts"][provider]["text"] if provider else ""
                counts[segment_state(meta)] += 1
                tfile = spath / "segments" / gid / "transcript.txt"
                new_text = text.rstrip() + ("\n" if text else "")
                if not tfile.exists() or tfile.read_text(encoding="utf-8") != new_text:
                    write_text(tfile, new_text)
                items.append((text, float(meta.get("gap_before_s") or 0.0) if len(items) else 0.0))
            joined = join_segment_texts(items, gap)
            write_text(spath / "transcript.txt", joined + ("\n" if joined else ""))
            smeta_path = spath / "meta.json"
            smeta = read_json(smeta_path)
            if smeta.get("counts") != counts:
                smeta["counts"] = counts
                write_json(smeta_path, smeta)
            session_texts.append(joined.strip())
        combined = "\n\n".join(t for t in session_texts if t)
        write_text(rpath / "transcript.txt", combined + ("\n" if combined else ""))
        _touch_recording(recording_id)
        return combined


def archive_segments(recording_id: str, session_id: str) -> Path | None:
    """Move the current segments of a session aside (used before re-cutting) and reset the counters."""
    with rec_lock(recording_id):
        spath = session_dir(recording_id, session_id)
        segments = spath / "segments"
        archived = None
        if _numeric_ids(segments):
            archive_root = spath / "_archive"
            archive_root.mkdir(exist_ok=True)
            archived = archive_root / f"segments-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            n = 1
            while archived.exists():
                archived = archive_root / f"segments-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{n}"
                n += 1
            segments.rename(archived)
        segments.mkdir(exist_ok=True)
        update_session(recording_id, session_id, segment_count=0, kept_seconds=0.0, counts={})
        return archived


# -- recovery and migration --------------------------------------------------------------------------


def recover_interrupted() -> dict[str, int]:
    """After a crash or restart: close sessions left in "recording" and mark unfinished jobs as interrupted."""
    ensure_dirs()
    fixed_sessions = fixed_jobs = 0
    for entry in RECORDINGS_DIR.iterdir():
        if not entry.is_dir() or not (entry / "meta.json").exists():
            continue
        try:
            rid = entry.name
            recording_dir(rid)
            for sid in _numeric_ids(entry / "sessions"):
                spath = entry / "sessions" / sid
                meta = read_json(spath / "meta.json")
                changed = False
                touched_session = False
                if (spath / "audio.pcm").exists():
                    pcm_to_wav(spath / "audio.pcm", spath / "audio.wav", SAMPLE_RATE)
                    (spath / "audio.pcm").unlink(missing_ok=True)
                    meta["recovered"] = True
                    changed = True
                if meta.get("status") in ("recording", "processing"):
                    meta["status"] = "ready"
                    meta["ended_at"] = meta.get("ended_at") or utc_now()
                    if (spath / "audio.wav").exists():
                        frames, rate = wav_frames(spath / "audio.wav")
                        meta["duration_seconds"] = round(frames / rate, 3)
                    changed = True
                    fixed_sessions += 1
                if changed:
                    write_json(spath / "meta.json", meta)
                for gid in _numeric_ids(spath / "segments"):
                    mpath = spath / "segments" / gid / "meta.json"
                    seg = read_json(mpath)
                    touched = False
                    for provider, tr in (seg.get("transcripts") or {}).items():
                        if tr.get("status") in ("queued", "running"):
                            tr["status"] = "error"
                            tr["error"] = {
                                "provider": provider,
                                "stage": "interrupted",
                                "type": "Interrupted",
                                "message": "Программа была закрыта, пока сегмент ждал расшифровки",
                                "hint": "Нажмите «Повторить».",
                                "retryable": True,
                                "at": utc_now(),
                            }
                            touched = True
                            fixed_jobs += 1
                    if touched:
                        write_json(mpath, seg)
                        touched_session = True
                if touched_session or changed:
                    rebuild_transcripts(rid, sid)
        except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError, KeyError):
            continue
    return {"sessions": fixed_sessions, "jobs": fixed_jobs}


def migrate_recording(path: Path) -> bool:
    """Bring a recording made by version 1.x (recording -> segments) to the current layout.

    The old "segment" (one press of start/stop) is now a *session*. Nothing is copied or deleted: the folder
    ``segments`` is renamed to ``sessions``, and each old segment becomes a session holding one segment that
    spans all of its audio, with the old transcript attached as an archived one (``V1_PROVIDER_ID``).
    Safe to run twice and after a crash.
    """
    meta_path = path / "meta.json"
    if not meta_path.exists():
        return False
    with rec_lock(path.name):
        meta = read_json(meta_path)
        legacy_dir, sessions_dir = path / "segments", path / "sessions"
        if meta.get("schema_version") == SCHEMA_VERSION and not legacy_dir.exists():
            return False
        if legacy_dir.is_dir() and not sessions_dir.exists():
            legacy_dir.rename(sessions_dir)
        sessions_dir.mkdir(exist_ok=True)
        for sid in _numeric_ids(sessions_dir):
            _migrate_session(path.name, sessions_dir / sid)
        old = {k: meta.pop(k) for k in ("segments", "segment_count", "files", "status", "duration_seconds", "model") if k in meta}
        meta.update(schema_version=SCHEMA_VERSION)
        if old:
            meta["legacy"] = old
        write_json(meta_path, meta)
        return True


def _migrate_session(recording_id: str, spath: Path) -> None:
    meta_path = spath / "meta.json"
    if not meta_path.exists():
        return
    old = read_json(meta_path)
    if old.get("schema_version") == SCHEMA_VERSION:
        return
    if (spath / "audio.pcm").exists() and not (spath / "audio.wav").exists():
        rate = int(old.get("sample_rate") or SAMPLE_RATE)
        pcm_to_wav(spath / "audio.pcm", spath / "audio.wav", rate)
        (spath / "audio.pcm").unlink(missing_ok=True)
    duration = float(old.get("duration_seconds") or 0.0)
    if (spath / "audio.wav").exists():
        frames, rate = wav_frames(spath / "audio.wav")
        duration = round(frames / rate, 3) if rate else duration
    seg_path = spath / "segments" / "001"
    seg_path.mkdir(parents=True, exist_ok=True)
    text = (spath / "transcript.txt").read_text(encoding="utf-8").strip() if (spath / "transcript.txt").exists() else ""
    status = old.get("status")
    raw_params = processing_from_preset("raw")
    transcripts: dict[str, Any] = {}
    if status == "completed" or text:
        transcripts[V1_PROVIDER_ID] = {
            "status": "done",
            "text": text,
            "model": old.get("model"),
            "elapsed_s": None,
            "finished_at": old.get("ended_at"),
            "processing": raw_params,
            "processing_label": "raw",
            "error": None,
            "migrated": True,
        }
    else:
        transcripts[V1_PROVIDER_ID] = {
            "status": "error",
            "text": "",
            "model": old.get("model"),
            "finished_at": old.get("ended_at"),
            "processing": raw_params,
            "processing_label": "raw",
            "error": {
                "provider": V1_PROVIDER_ID,
                "stage": "transcribe",
                "type": "LegacyError",
                "message": old.get("error") or "Расшифровка не была завершена",
                "hint": "Нажмите «Повторить» или выберите другую модель.",
                "retryable": True,
                "at": old.get("ended_at"),
                "context": {"legacy_status": status},
                "caused_by": [],
                "traceback": "",
            },
        }
    if (spath / "result.json").exists():
        shutil.move(str(spath / "result.json"), str(seg_path / f"{V1_PROVIDER_ID}.json"))
    seg_meta = {
        "id": "001",
        "session_id": spath.name,
        "recording_id": recording_id,
        "index": 0,
        "start_s": 0.0,
        "end_s": duration,
        "duration_seconds": duration,
        "speech_s": None,
        "gap_before_s": 0.0,
        "cut_reason": "single",
        "created_at": old.get("ended_at") or utc_now(),
        "processing": raw_params,
        "processing_label": "raw",
        "audio_params": None,
        "audio": {"original": None, "processed": None, "steps": [], "warnings": []},
        "transcripts": transcripts,
        "selected": None,
    }
    write_json(seg_path / "meta.json", seg_meta)
    write_text(seg_path / "transcript.txt", text + ("\n" if text else ""))
    slicing = slicing_from_preset("off")
    session_meta = {
        "schema_version": SCHEMA_VERSION,
        "id": spath.name,
        "recording_id": recording_id,
        "source": "migrated",
        "started_at": old.get("started_at"),
        "ended_at": old.get("ended_at"),
        "status": "ready",
        "sample_rate": int(old.get("sample_rate") or SAMPLE_RATE),
        "device_sample_rate": old.get("device_sample_rate"),
        "duration_seconds": duration,
        "language": old.get("language") or "ru",
        "slicing": slicing,
        "slicing_label": "off",
        "processing": raw_params,
        "processing_label": "raw",
        "targets": [],
        "mic": {},
        "segment_count": 1,
        "kept_seconds": duration,
        "counts": {"working": 0, "error": 0 if transcripts[V1_PROVIDER_ID]["status"] == "done" else 1,
                   "done": 1 if transcripts[V1_PROVIDER_ID]["status"] == "done" else 0, "idle": 0},
        "noise_floor_db": None,
        "error": None,
    }
    shutil.move(str(meta_path), str(spath / "legacy_meta.json"))
    write_json(meta_path, session_meta)


def migrate_all() -> int:
    ensure_dirs()
    done = 0
    for entry in sorted(RECORDINGS_DIR.iterdir()):
        if entry.is_dir() and (entry / "meta.json").exists():
            try:
                if migrate_recording(entry):
                    done += 1
                _migrated.add(entry.name)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    return done
