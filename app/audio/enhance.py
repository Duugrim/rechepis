"""Level handling: rumble filter, loudness normalisation, peak limiter and measurements."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

from . import SAMPLE_RATE
from .vad import DIGITAL_SILENCE_DB, FRAME_LEN, frames_of

PEAK_CEILING = 10 ** (-1.0 / 20)  # -1 dBFS
CLIP_LEVEL = 0.999
MIN_REASONABLE_GAIN_DB = -12.0  # never turn a loud recording down by more than this
SPEECH_WINDOW_DB = 15.0
MIN_SIGNAL_DBFS = -70.0  # a signal whose loud frames are quieter than this is treated as silence
NOISE_FRAMES = 10  # 200 ms


def db(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-9)))


def highpass(x: np.ndarray, cutoff_hz: float) -> np.ndarray:
    if cutoff_hz <= 0 or len(x) < 64:
        return x
    sos = butter(4, cutoff_hz, btype="highpass", fs=SAMPLE_RATE, output="sos")
    return sosfiltfilt(sos, x.astype(np.float64)).astype(np.float32)


def soft_limit(x: np.ndarray, ceiling: float = PEAK_CEILING) -> np.ndarray:
    """Transparent below the knee, smoothly bends peaks into the ceiling instead of hard clipping."""
    knee = ceiling * 0.8
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    y = x.copy()
    room = ceiling - knee
    y[over] = np.sign(x[over]) * (knee + room * np.tanh((a[over] - knee) / room))
    return y


def analyze(x: np.ndarray) -> dict:
    """Numbers the UI shows and the quality warnings are derived from.

    Self-contained on purpose: a segment holds little more than speech, so "speech" is taken as the frames
    within ``SPEECH_WINDOW_DB`` of the loud ones, and the noise as the quietest 200 ms (the padding at the
    segment's edges is room tone by construction).
    """
    empty = {"peak_dbfs": None, "speech_rms_dbfs": None, "noise_dbfs": None, "snr_db": None, "clipped_ratio": 0.0, "speech_ratio": 0.0}
    if len(x) == 0:
        return empty
    stats = {**empty, "peak_dbfs": round(db(float(np.abs(x).max())), 1), "clipped_ratio": round(float((np.abs(x) >= CLIP_LEVEL).mean()), 5)}
    if len(x) < FRAME_LEN * 10:
        return stats
    power = np.mean(np.square(frames_of(x), dtype=np.float32), axis=1)
    levels = 10 * np.log10(power + 1e-10)
    live = levels > DIGITAL_SILENCE_DB
    if live.sum() < 10:
        return stats
    loud = float(np.percentile(levels[live], 90))
    if loud < MIN_SIGNAL_DBFS:
        return stats
    mask = levels >= loud - SPEECH_WINDOW_DB
    stats["speech_rms_dbfs"] = round(10 * np.log10(float(power[mask].mean()) + 1e-12), 1)
    stats["speech_ratio"] = round(float((levels >= loud - 25).mean()), 3)
    if len(power) >= NOISE_FRAMES:
        quietest = np.convolve(power, np.ones(NOISE_FRAMES) / NOISE_FRAMES, mode="valid").min()
        noise_db = 10 * np.log10(float(quietest) + 1e-12)
        stats["noise_dbfs"] = round(noise_db, 1)
        stats["snr_db"] = round(stats["speech_rms_dbfs"] - noise_db, 1)
    return stats


def normalize(x: np.ndarray, target_dbfs: float, max_gain_db: float, speech_rms_dbfs: float | None) -> tuple[np.ndarray, float]:
    """Bring the speech to ``target_dbfs`` RMS (capped so noise is not blown up), then limit the peaks."""
    if speech_rms_dbfs is None or len(x) == 0:
        return x, 0.0
    gain_db = float(np.clip(target_dbfs - speech_rms_dbfs, MIN_REASONABLE_GAIN_DB, max_gain_db))
    # Let the limiter work on at most ~6 dB of overshoot: beyond that the picture is a spiky signal and a
    # lower gain sounds (and recognises) better than a heavily squashed one.
    peak_db = db(float(np.percentile(np.abs(x), 99.95)))
    gain_db = min(gain_db, (-1.0 - peak_db) + 6.0)
    y = soft_limit(x * np.float32(10 ** (gain_db / 20)))
    return y.astype(np.float32), gain_db
