import numpy as np
import pytest

from app.audio.enhance import analyze, normalize, soft_limit
from app.audio.pipeline import process_audio, warnings_for
from app.audio.resample import StreamResampler, resample
from app.audio.segmenter import Segmenter, cut_audio, single_cut
from app.settings import normalize_processing, normalize_slicing, processing_from_preset

from .synth import SR, dictation, room, speech


@pytest.mark.parametrize("rate", [48000, 44100, 32000, 22050, 16000])
def test_stream_resampler_equals_one_shot(rate):
    rng = np.random.default_rng(3)
    t = np.arange(rate * 2 + 77) / rate
    x = (0.4 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(len(t))).astype(np.float32)
    expected = resample(x, rate, 16000)
    r = StreamResampler(rate, 16000)
    pieces, i = [], 0
    while i < len(x):
        step = int(rng.integers(50, 4000))
        pieces.append(r.process(x[i : i + step]))
        i += step
    pieces.append(r.process(np.zeros(0, np.float32), final=True))
    got = np.concatenate(pieces)
    assert len(got) == len(expected)
    assert np.abs(got - expected).max() < 1e-5


def test_resampling_removes_aliasing():
    # A 12 kHz tone sampled at 48 kHz must vanish, not fold down to 4 kHz as it did with linear interpolation.
    t = np.arange(48000) / 48000
    y = resample(np.sin(2 * np.pi * 12000 * t).astype(np.float32), 48000, 16000)
    assert np.abs(y[200:-200]).max() < 0.02


def test_cuts_at_pauses_and_drops_long_silence():
    x = dictation([("room", 1), ("speech", 14), ("room", 2), ("speech", 3), ("room", 5), ("speech", 4), ("room", 1)])
    cuts = cut_audio(x, normalize_slicing({}))
    assert len(cuts) == 3
    first = cuts[0]
    assert first.start / SR == pytest.approx(0.75, abs=0.1)  # speech starts at 1 s, minus 250 ms of padding
    assert first.end / SR == pytest.approx(15.25, abs=0.2)
    assert cuts[1].gap_before_s > 1.0
    assert cuts[2].gap_before_s > 4.0  # the 5 s thinking pause is dropped
    assert all(c.end > c.start for c in cuts)
    assert all(a.end <= b.start for a, b in zip(cuts, cuts[1:]))


def test_live_feed_gives_the_same_cuts_as_offline():
    x = dictation([("room", 1), ("speech", 14), ("room", 2), ("speech", 3), ("room", 5), ("speech", 4), ("room", 1)])
    settings = normalize_slicing({})
    rng = np.random.default_rng(0)
    seg, live, i = Segmenter(settings), [], 0
    while i < len(x):
        n = int(rng.integers(100, 9000))
        live += seg.feed(x[i : i + n])
        i += n
    live += seg.flush()
    offline = cut_audio(x, settings)
    assert len(live) == len(offline)
    for a, b in zip(live, offline):
        assert abs(a.start - b.start) < SR * 0.1 and abs(a.end - b.end) < SR * 0.1


def test_continuous_speech_is_split_at_max_length():
    x = np.concatenate([room(1), speech(60), room(1)])
    settings = normalize_slicing({"max_len_s": 20, "target_len_s": 10})
    cuts = cut_audio(x, settings)
    assert len(cuts) >= 3
    assert all(c.duration_s <= 20.3 for c in cuts)
    assert any(c.forced for c in cuts)
    # nothing is lost: the pieces tile the speech without gaps
    covered = sum(c.end - c.start for c in cuts)
    assert covered >= 59.5 * SR


def test_dips_count_as_pauses_when_there_is_no_true_silence():
    # Browser noise suppression leaves a hum between phrases instead of silence.
    quiet_hum = speech(0.9, level=0.008, seed=9)
    parts = [room(1), speech(7), quiet_hum, speech(7), quiet_hum, speech(7), room(1)]
    cuts = cut_audio(np.concatenate(parts), normalize_slicing({"target_len_s": 6}))
    assert len(cuts) >= 3
    assert not any(c.forced for c in cuts)


def test_silence_only_gives_no_segments():
    assert cut_audio(room(10), normalize_slicing({})) == []
    assert single_cut(0) == []


def test_normalize_brings_quiet_speech_to_target():
    x = np.concatenate([room(1, level=0.0003), speech(6, level=0.01), room(1, level=0.0003)])
    stats = analyze(x)
    assert stats["speech_rms_dbfs"] < -38
    assert "quiet" in warnings_for(stats)
    y, report = process_audio(x, processing_from_preset("light"))
    assert report["processed"]["speech_rms_dbfs"] == pytest.approx(-20, abs=1.5)
    assert np.abs(y).max() <= 1.0


def test_gain_is_capped_and_peaks_limited():
    loud = np.clip(speech(3, level=0.95), -1, 1)
    out, gain = normalize(loud, -20.0, 30.0, analyze(loud)["speech_rms_dbfs"])
    assert gain < 0 and np.abs(out).max() <= 0.9
    assert np.abs(soft_limit(np.array([2.0, -2.0, 0.1], dtype=np.float32))).max() < 1.0


def test_denoise_lowers_the_noise_floor_and_keeps_speech():
    rng = np.random.default_rng(5)
    body = np.concatenate([speech(4, level=0.1), np.zeros(SR, np.float32)])
    noise = (rng.standard_normal(SR + len(body)) * 0.006).astype(np.float32)
    # a second of noise only in front, as at the start of a real recording
    noisy = np.concatenate([np.zeros(SR, np.float32), body]) + noise
    out, report = process_audio(noisy, normalize_processing({"highpass_hz": 0, "denoise": 0.8, "normalize": False}))
    before, after = analyze(noisy), analyze(out)
    assert after["noise_dbfs"] < before["noise_dbfs"] - 6
    assert after["speech_rms_dbfs"] > before["speech_rms_dbfs"] - 3  # speech survives
    assert any(s["step"] == "denoise" and s["applied"] for s in report["steps"])


def test_denoise_leaves_digital_silence_alone():
    x = np.concatenate([np.zeros(SR, np.float32), speech(3), np.zeros(SR, np.float32)])
    out, report = process_audio(x, normalize_processing({"highpass_hz": 0, "denoise": 0.8, "normalize": False}))
    assert np.abs(out - x).max() < 0.05
    assert np.isfinite(out).all()


def test_raw_preset_is_identity():
    x = speech(2)
    y, report = process_audio(x, processing_from_preset("raw"))
    assert np.array_equal(x, y) and report["steps"] == []
