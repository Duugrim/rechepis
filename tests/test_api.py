import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, providers, store
from app.audio.resample import resample
from app.providers.base import Provider, ProviderError, TranscriptResult

from .synth import dictation, pcm16
from .test_store import make_legacy_recording


class FakeProvider(Provider):
    kind = "local"

    def __init__(self, pid, text, fail=None):
        self.id, self.label, self.text, self.fail = pid, pid.title(), text, fail
        self.calls = []

    def status(self):
        return {"id": self.id, "label": self.label, "kind": self.kind, "ready": True, "state": "ready", "message": "ok"}

    async def transcribe(self, audio_path, *, language, duration_s):
        self.calls.append((audio_path.name, duration_s))
        await asyncio.sleep(0.01)
        if self.fail:
            raise self.fail
        return TranscriptResult(text=f"{self.text} {len(self.calls)}", model="fake", elapsed_s=0.01)


@pytest.fixture()
def client():
    providers.get_providers()
    saved = dict(providers._providers)
    providers._providers["gigaam"] = FakeProvider("gigaam", "гига")
    with TestClient(__import__("app.main", fromlist=["app"]).app) as c:
        yield c
    providers._providers.clear()
    providers._providers.update(saved)


def wait_until(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def record(client, rid, parts, targets, slicing=None):
    """Play a dictation through /ws/capture the way the browser does: 48 kHz int16 in small chunks."""
    audio48 = resample(dictation(parts), 16000, 48000)
    data = pcm16(audio48)
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"action": "start", "recording_id": rid, "sample_rate": 48000, "targets": targets, **({"slicing": slicing} if slicing else {})})
        msg = ws.receive_json()
        while msg["type"] != "started":
            msg = ws.receive_json()
        session_id = msg["session"]["id"]
        chunk = 4800 * 2
        for i in range(0, len(data), chunk):
            ws.send_bytes(data[i : i + chunk])
        ws.send_json({"action": "stop"})
        while msg["type"] != "session_done":
            msg = ws.receive_json()
    return session_id, msg["session"]


PARTS = [("room", 1), ("speech", 14), ("room", 2), ("speech", 3), ("room", 5), ("speech", 4), ("room", 1)]


def test_settings_of_a_dropped_cloud_provider_are_ignored_and_purged(client):
    # a config.json written by an older version: it still holds an API token and the old provider settings
    config.CONFIG_PATH.write_text(
        json.dumps({"token": "secret-token-1234567890", "model": "old-cloud-model", "preferred_provider": "old-cloud",
                    "targets": ["old-cloud", "gigaam"], "language": "ru"}),
        encoding="utf-8",
    )
    body = client.get("/api/config").json()
    assert "secret-token" not in str(body)
    for gone in ("token", "model", "preferred_provider", "palatine"):
        assert gone not in body["config"]
    assert body["config"]["targets"] == ["gigaam"] and list(body["providers"]) == ["gigaam"]
    # the first save rewrites the file without the token
    client.put("/api/config", json={"token": "ignored", "paragraph_gap_s": 2})
    saved = config.CONFIG_PATH.read_text(encoding="utf-8")
    assert "secret-token" not in saved and "ignored" not in saved and "token" not in json.loads(saved)


def test_cloud_endpoints_are_gone(client):
    rid = client.post("/api/recordings", json={"title": "x"}).json()["id"]
    assert client.post(f"/api/recordings/{rid}/summarize", json={}).status_code in (404, 405)
    assert client.get(f"/api/recordings/{rid}/summary").status_code in (404, 405)


def test_config_values_are_validated(client):
    body = client.put("/api/config", json={"slicing": {"max_len_s": 999999, "min_len_s": -5}, "targets": ["gigaam", "bogus"]}).json()
    assert body["config"]["slicing"]["max_len_s"] == 300
    assert body["config"]["slicing"]["min_len_s"] == 0.5
    assert body["config"]["targets"] == ["gigaam"]


def test_live_session_is_cut_registered_and_transcribed(client):
    rid = client.post("/api/recordings", json={"title": "Тест"}).json()["id"]
    sid, done = record(client, rid, PARTS, targets=["gigaam"])

    assert done["status"] == "ready" and done["segment_count"] == 3
    assert done["duration_seconds"] == pytest.approx(30.0, abs=0.2)
    assert done["device_sample_rate"] == 48000

    def all_done():
        rec = client.get(f"/api/recordings/{rid}").json()
        segs = rec["sessions"][0]["segments"]
        # the combined text is rebuilt right after a segment turns "done" (the UI gets it as its own event)
        return rec if segs and all(s["state"] == "done" for s in segs) and rec["transcript"].count("гига") == 3 else None

    rec = wait_until(all_done)
    segs = rec["sessions"][0]["segments"]
    assert [s["text"].split()[0] for s in segs] == ["гига"] * 3
    assert segs[0]["audio"]["original"]["speech_rms_dbfs"] is not None
    assert rec["transcript"].count("гига") == 3
    assert "\n\n" in rec["transcript"]  # the 5 s pause starts a new paragraph
    # the raw capture is kept as one wav; segments are slices of it
    audio = client.get(f"/api/recordings/{rid}/sessions/{sid}/audio")
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    peaks = client.get(f"/api/recordings/{rid}/sessions/{sid}/peaks?buckets=100").json()
    assert len(peaks["peaks"]) == 100


def test_archived_transcript_stays_readable_and_can_be_picked(client):
    rid = client.post("/api/recordings", json={"title": "Архив"}).json()["id"]
    sid, _ = record(client, rid, PARTS, targets=[])
    segs = client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"]
    assert segs and all(s["state"] == "idle" for s in segs)  # nothing was sent automatically

    first = segs[0]["id"]
    r = client.post(f"/api/recordings/{rid}/sessions/{sid}/segments/{first}/transcribe", json={"providers": ["gigaam"]})
    assert r.json()["queued"] == ["gigaam"]

    def first_segment():
        return client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"][0]

    wait_until(lambda: first_segment()["state"] == "done")
    # a transcript left by a provider this version no longer ships (an old recording has nothing else)
    store.set_transcript(rid, sid, first, store.V1_PROVIDER_ID, {"status": "done", "text": "старый текст"})
    seg = first_segment()
    assert seg["provider"] == "gigaam" and set(seg["transcripts"]) == {"gigaam", store.V1_PROVIDER_ID}

    url = f"/api/recordings/{rid}/sessions/{sid}/segments/{first}"
    client.patch(url, json={"selected": store.V1_PROVIDER_ID})
    seg = first_segment()
    assert seg["provider"] == store.V1_PROVIDER_ID and seg["text"] == "старый текст"
    assert client.get(f"/api/recordings/{rid}").json()["transcript"].startswith("старый текст")
    client.patch(url, json={"selected": None})
    assert first_segment()["provider"] == "gigaam"
    assert client.patch(url, json={"selected": "bogus"}).status_code == 400
    # nothing can be sent to a provider that is gone
    r = client.post(f"{url}/transcribe", json={"providers": [store.V1_PROVIDER_ID]})
    assert r.status_code == 400


def test_failure_keeps_full_error_details_and_retry_works(client):
    providers._providers["gigaam"].fail = ProviderError(
        "Модель ответила ошибкой", stage="transcribe", http_status=503, body='{"detail":"overloaded"}'
    )
    rid = client.post("/api/recordings", json={"title": "Ошибка"}).json()["id"]
    sid, _ = record(client, rid, PARTS[:3], targets=["gigaam"])

    def errored():
        segs = client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"]
        return segs if segs and all(s["state"] == "error" for s in segs) else None

    segs = wait_until(errored)
    err = segs[0]["transcripts"]["gigaam"]["error"]
    assert err["http_status"] == 503 and err["body"] == '{"detail":"overloaded"}'
    assert err["stage"] == "transcribe" and err["type"] == "ProviderError"
    assert "позже" in err["hint"]
    assert err["context"]["segment"].startswith(sid) and err["traceback"]

    providers._providers["gigaam"].fail = None
    r = client.post(f"/api/recordings/{rid}/sessions/{sid}/transcribe", json={"providers": ["gigaam"], "only": "errors"})
    assert r.json()["queued"] == len(segs)
    wait_until(lambda: all(s["state"] == "done" for s in client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"]))


def test_resend_with_other_processing_replaces_the_audio(client):
    rid = client.post("/api/recordings", json={"title": "Обработка"}).json()["id"]
    sid, _ = record(client, rid, PARTS[:3], targets=[])
    gid = client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"][0]["id"]
    for preset in ("raw", "strong"):
        client.post(f"/api/recordings/{rid}/sessions/{sid}/segments/{gid}/transcribe", json={"providers": ["gigaam"], "preset": preset})
        wait_until(lambda p=preset: (lambda s: s["state"] == "done" and s["processing_label"] == p)(
            client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"][0]))
    seg = client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"][0]
    assert seg["transcripts"]["gigaam"]["processing_label"] == "strong"
    assert any(step["step"] == "denoise" for step in seg["audio"]["steps"])
    raw = client.get(f"/api/recordings/{rid}/sessions/{sid}/segments/{gid}/audio?kind=raw")
    processed = client.get(f"/api/recordings/{rid}/sessions/{sid}/segments/{gid}/audio?kind=processed")
    assert raw.status_code == processed.status_code == 200 and raw.content != processed.content


def test_resegment_previews_then_replaces_segments(client):
    rid = client.post("/api/recordings", json={"title": "Нарезка"}).json()["id"]
    sid, _ = record(client, rid, PARTS, targets=[])
    url = f"/api/recordings/{rid}/sessions/{sid}/resegment"

    preview = client.post(url, json={"slicing": {"enabled": False}, "dry_run": True}).json()
    assert preview["summary"]["count"] == 1
    assert len(client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"]) == 3  # nothing changed

    r = client.post(url, json={"slicing_preset": "short", "targets": ["gigaam"]})
    assert r.status_code == 200
    segs = wait_until(lambda: (lambda s: s if s["status"] == "ready" and s["segments"] and all(x["state"] == "done" for x in s["segments"]) else None)(
        client.get(f"/api/recordings/{rid}").json()["sessions"][0]))
    assert segs["slicing_label"] == "short" and len(segs["segments"]) >= 3
    assert list((store.session_dir(rid, sid) / "_archive").iterdir())  # the old cut is kept aside


def test_unready_provider_is_refused_up_front(client):
    class Down(FakeProvider):
        def status(self):
            return {**super().status(), "ready": False, "message": "Модель не скачана"}

    providers._providers["gigaam"] = Down("gigaam", "x")
    rid = client.post("/api/recordings", json={"title": "x"}).json()["id"]
    sid, _ = record(client, rid, PARTS[:3], targets=[])
    gid = client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"][0]["id"]
    r = client.post(f"/api/recordings/{rid}/sessions/{sid}/segments/{gid}/transcribe", json={"providers": ["gigaam"]})
    assert r.status_code == 400 and "Модель не скачана" in r.json()["detail"]


def test_slicing_off_keeps_the_whole_session_as_one_segment(client):
    rid = client.post("/api/recordings", json={"title": "Целиком"}).json()["id"]
    sid, done = record(client, rid, PARTS, targets=["gigaam"], slicing={"enabled": False})
    assert done["segment_count"] == 1 and done["slicing"]["enabled"] is False
    seg = client.get(f"/api/recordings/{rid}").json()["sessions"][0]["segments"][0]
    assert seg["cut_reason"] == "single" and seg["duration_seconds"] == pytest.approx(30.0, abs=0.2)


def test_silent_session_leaves_a_session_without_segments(client):
    rid = client.post("/api/recordings", json={"title": "Тишина"}).json()["id"]
    sid, done = record(client, rid, [("room", 6)], targets=["gigaam"])
    assert done["segment_count"] == 0 and done["status"] == "ready"


def test_instant_stop_leaves_no_session(client):
    rid = client.post("/api/recordings", json={"title": "Пусто"}).json()["id"]
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"action": "start", "recording_id": rid, "sample_rate": 48000})
        assert ws.receive_json()["type"] == "started"
        ws.send_json({"action": "stop"})
    time.sleep(0.5)
    assert client.get(f"/api/recordings/{rid}").json()["sessions"] == []


def test_old_recordings_show_up_migrated(client):
    root = make_legacy_recording("2026-08-01_120000_old")
    listing = client.get("/api/recordings").json()["recordings"]
    item = next(r for r in listing if r["id"] == root.name)
    assert item["session_count"] == 2 and item["segment_count"] == 2
    rec = client.get(f"/api/recordings/{root.name}").json()
    assert rec["sessions"][0]["segments"][0]["text"] == "текст сегмента 1"
    # a migrated 2 s "session" can be re-cut and sent to the new model like any other
    r = client.post(f"/api/recordings/{root.name}/sessions/001/transcribe", json={"providers": ["gigaam"], "only": "all"})
    assert r.json()["queued"] == 1
