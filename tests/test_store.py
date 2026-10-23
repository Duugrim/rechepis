import json
import wave

import numpy as np
import pytest

from app import config, store

from .synth import SR, pcm16, speech


def make_legacy_recording(name="2026-08-01_120000_old", *, completed=True, sessions=2):
    """A recording as version 1.x wrote it: recording -> segments (the new sessions)."""
    root = config.RECORDINGS_DIR / name
    root.mkdir(parents=True)
    metas = []
    texts = []
    for n in range(1, sessions + 1):
        sid = f"{n:03d}"
        seg = root / "segments" / sid
        seg.mkdir(parents=True)
        audio = speech(2.0, seed=n)
        with wave.open(str(seg / "audio.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm16(audio))
        ok = completed or n > 1
        text = f"текст сегмента {n}" if ok else ""
        (seg / "transcript.txt").write_text(text + ("\n" if text else ""), encoding="utf-8")
        (seg / "result.json").write_text(json.dumps({"text": text, "segments": []}), encoding="utf-8")
        meta = {
            "id": sid, "recording_id": name, "started_at": "2026-08-01T10:00:00+00:00",
            "ended_at": "2026-08-01T10:00:02+00:00", "status": "completed" if ok else "error",
            "sample_rate": SR, "channels": 1, "language": "ru", "model": "v1-model",
            "duration_seconds": 2.0, "bytes_written": SR * 4, "error": None if ok else "HTTP 500", "device_sample_rate": 48000,
        }
        (seg / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        metas.append({k: meta[k] for k in ("id", "started_at", "ended_at", "duration_seconds", "status", "error")})
        if text:
            texts.append(text)
    rec_meta = {
        "id": name, "title": "Старая запись", "created_at": "2026-08-01T10:00:00+00:00",
        "updated_at": "2026-08-01T10:00:02+00:00", "status": "open", "language": "ru",
        "model": "v1-model", "segment_count": sessions, "duration_seconds": 2.0 * sessions,
        "has_summary": False, "segments": metas, "files": {"transcript": "transcript.txt"},
    }
    (root / "meta.json").write_text(json.dumps(rec_meta), encoding="utf-8")
    (root / "transcript.txt").write_text("\n\n".join(texts) + "\n", encoding="utf-8")
    return root


def test_legacy_recording_is_migrated_without_touching_audio():
    root = make_legacy_recording()
    audio_before = (root / "segments" / "001" / "audio.wav").read_bytes()
    transcript_before = (root / "transcript.txt").read_text(encoding="utf-8")

    rec = store.get_recording(root.name)

    assert not (root / "segments").exists()
    assert (root / "sessions" / "001" / "audio.wav").read_bytes() == audio_before
    assert (root / "sessions" / "001" / "legacy_meta.json").exists()
    assert rec["meta"]["schema_version"] == 2
    assert rec["transcript"] == transcript_before
    assert [s["id"] for s in rec["sessions"]] == ["001", "002"]
    session = rec["sessions"][0]
    assert session["source"] == "migrated" and session["segment_count"] == 1
    segment = session["segments"][0]
    assert segment["text"] == "текст сегмента 1" and segment["provider"] == store.V1_PROVIDER_ID
    assert segment["end_s"] == pytest.approx(2.0, abs=0.01)
    assert (root / "sessions" / "001" / "segments" / "001" / f"{store.V1_PROVIDER_ID}.json").exists()


def test_rebuild_keeps_the_text_of_archived_transcripts():
    """Segments of old recordings have no other text: a rebuild (any later edit does one) must not blank them."""
    root = make_legacy_recording()
    before = store.get_recording(root.name)["transcript"].strip()
    assert "текст сегмента 1" in before and "текст сегмента 2" in before

    assert store.rebuild_transcripts(root.name) == before
    seg_file = root / "sessions" / "001" / "segments" / "001" / "transcript.txt"
    assert seg_file.read_text(encoding="utf-8").strip() == "текст сегмента 1"
    assert (root / "transcript.txt").read_text(encoding="utf-8").strip() == before


def test_migration_is_idempotent_and_resumable():
    root = make_legacy_recording()
    # crash after the folder rename but before the sessions were converted
    (root / "segments").rename(root / "sessions")
    store.recording_dir(root.name)
    store._migrated.clear()
    first = store.get_recording(root.name)
    store._migrated.clear()
    assert store.migrate_recording(root) is False
    assert store.get_recording(root.name)["sessions"][0]["segments"][0]["text"] == first["sessions"][0]["segments"][0]["text"]


def test_failed_legacy_segment_becomes_a_retryable_error():
    root = make_legacy_recording(completed=False)
    segment = store.get_recording(root.name)["sessions"][0]["segments"][0]
    assert segment["state"] == "error"
    assert segment["transcripts"][store.V1_PROVIDER_ID]["error"]["message"] == "HTTP 500"
    assert segment["transcripts"][store.V1_PROVIDER_ID]["error"]["retryable"] is True


def _cut(start, end, gap=0.0):
    return {"start": int(start * SR), "end": int(end * SR), "reason": "pause", "speech_s": end - start, "gap_before_s": gap}


def _session_with_segments(n=3, gaps=(0.0, 0.5, 3.0)):
    rec = store.create_recording("Тема")
    cfg = config.load_config()
    session = store.create_session(
        rec["id"], slicing=cfg["slicing"], processing=cfg["processing"], targets=[], language="ru"
    )
    sid = session["id"]
    # the raw audio of the session: 3 x 4 s of speech
    from app.audio.wavio import write_wav

    write_wav(store.session_dir(rec["id"], sid) / "audio.wav", np.concatenate([speech(4, seed=i) for i in range(n)]))
    store.update_session(rec["id"], sid, status="ready", duration_seconds=4.0 * n)
    ids = []
    for i in range(n):
        meta = store.add_segment(rec["id"], sid, _cut(i * 4, i * 4 + 4, gaps[i]), processing=cfg["processing"])
        ids.append(meta["id"])
    return rec["id"], sid, ids


def test_transcript_joins_segments_by_gap_and_honours_a_pinned_archived_text():
    rid, sid, ids = _session_with_segments()
    for i, gid in enumerate(ids):
        store.set_transcript(rid, sid, gid, "gigaam", {"status": "done", "text": f"giga {i}"}, rebuild=False)
    store.set_transcript(rid, sid, ids[1], store.V1_PROVIDER_ID, {"status": "done", "text": "old 1"})
    # default: the supported provider wins; gaps 0.5 s join into a sentence flow, 3 s starts a paragraph
    assert store.get_recording(rid)["transcript"].strip() == "giga 0 giga 1\n\ngiga 2"
    store.set_selected(rid, sid, ids[1], store.V1_PROVIDER_ID)
    assert store.get_recording(rid)["transcript"].strip() == "giga 0 old 1\n\ngiga 2"
    # a pin whose transcript is no longer usable falls back instead of showing nothing
    store.set_transcript(rid, sid, ids[1], store.V1_PROVIDER_ID, {"status": "error", "text": ""})
    assert store.get_recording(rid)["transcript"].strip() == "giga 0 giga 1\n\ngiga 2"
    assert store.segment_public(rid, sid, ids[1])["provider"] == "gigaam"


def test_only_an_existing_transcript_can_be_pinned():
    rid, sid, ids = _session_with_segments()
    with pytest.raises(ValueError):
        store.set_selected(rid, sid, ids[0], store.V1_PROVIDER_ID)  # this segment has no such transcript
    with pytest.raises(ValueError):
        store.set_selected(rid, sid, ids[0], "bogus")
    store.set_selected(rid, sid, ids[0], "gigaam")  # a supported provider can be pinned before it has answered
    store.set_selected(rid, sid, ids[0], None)


def test_prepare_segment_audio_caches_and_reprocesses():
    rid, sid, ids = _session_with_segments()
    first = store.prepare_segment_audio(rid, sid, ids[0])
    stamp = first.stat().st_mtime_ns
    assert store.prepare_segment_audio(rid, sid, ids[0]).stat().st_mtime_ns == stamp  # cached
    raw = {"highpass_hz": 0, "denoise": 0, "normalize": False}
    store.prepare_segment_audio(rid, sid, ids[0], raw)
    meta = store.read_segment_meta(rid, sid, ids[0])
    assert meta["processing_label"] == "raw" and meta["audio"]["steps"] == []
    assert meta["duration_seconds"] == pytest.approx(4.0)


def test_archive_keeps_old_segments_when_recutting():
    rid, sid, ids = _session_with_segments()
    archived = store.archive_segments(rid, sid)
    assert archived is not None and (archived / ids[0] / "meta.json").exists()
    assert store.get_session(rid, sid)["segments"] == []
    assert store.read_session_meta(rid, sid)["segment_count"] == 0


def test_recover_closes_interrupted_sessions_and_jobs():
    rid, sid, ids = _session_with_segments(n=1, gaps=(0.0,))
    spath = store.session_dir(rid, sid)
    (spath / "audio.wav").unlink()
    (spath / "audio.pcm").write_bytes(pcm16(speech(3)))
    store.update_session(rid, sid, status="recording")
    store.set_transcript(rid, sid, ids[0], "gigaam", {"status": "running"})
    store._migrated.clear()
    fixed = store.recover_interrupted()
    assert fixed == {"sessions": 1, "jobs": 1}
    session = store.get_session(rid, sid)
    assert session["status"] == "ready" and session["recovered"] and (spath / "audio.wav").exists()
    assert session["segments"][0]["transcripts"]["gigaam"]["error"]["type"] == "Interrupted"


def test_unsafe_ids_are_rejected():
    for bad in ("..", "../x", "a/b"):
        with pytest.raises((ValueError, FileNotFoundError)):
            store.recording_dir(bad)
