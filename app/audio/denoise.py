"""Stationary noise suppression: the classic "learn the noise from the pauses" approach.

1. Find the frames of the recording that contain only the room (the quietest ones; speech is much louder)
   and average their power spectrum: that is the noise profile.
2. Run a decision-directed Wiener filter (Ephraim-Malah) frame by frame. Compared to plain spectral
   subtraction it leaves far less "musical" residue, which matters because recognisers are more upset by
   artefacts than by steady hiss.
3. Never cut deeper than a floor gain, so speech keeps its natural background and nothing is gated to
   digital silence.

Only steady noise (fans, mains hum, hiss) is removed. Other voices or keyboard clatter are not stationary
and pass through, which is the safe failure mode.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import ShortTimeFFT

from . import SAMPLE_RATE

N_FFT = 512
HOP = 128
SMOOTHING = 0.98  # weight of the previous frame in the decision-directed a priori SNR
MIN_NOISE_FRAMES = 20  # ~160 ms; less than that is not a usable profile
NOISE_PERCENTILE = 10
NOISE_MARGIN_DB = 4.0
MAX_USABLE_NOISE_DB = -32.0  # a "noise" louder than this is probably speech everywhere: do not touch the audio

_sft = ShortTimeFFT.from_window("hann", SAMPLE_RATE, N_FFT, N_FFT - HOP, fft_mode="onesided", scale_to=None)


def estimate_noise_psd(spec: np.ndarray) -> tuple[np.ndarray | None, float]:
    """Mean power spectrum of the quietest frames, and their level in dB (relative to full scale)."""
    power = np.abs(spec) ** 2  # (bins, frames)
    frame_power = power.sum(axis=0) / (N_FFT * 0.375 * N_FFT / 2)  # ~ mean square of the windowed frame
    level_db = 10 * np.log10(frame_power + 1e-12)
    floor_db = float(np.percentile(level_db, NOISE_PERCENTILE))
    quiet = level_db <= floor_db + NOISE_MARGIN_DB
    if quiet.sum() < MIN_NOISE_FRAMES:
        order = np.argsort(level_db)[:MIN_NOISE_FRAMES]
        quiet = np.zeros_like(quiet)
        quiet[order] = True
    if quiet.sum() < MIN_NOISE_FRAMES:
        return None, floor_db
    return power[:, quiet].mean(axis=1), floor_db


def denoise(x: np.ndarray, strength: float, context: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """Suppress steady noise in ``x``. ``context`` (more of the same recording) improves the noise profile."""
    info: dict = {"applied": False}
    if strength <= 0 or len(x) < N_FFT * 4:
        return x, info
    strength = float(min(strength, 1.0))
    spec = _sft.stft(x.astype(np.float64))
    noise_source = _sft.stft(context.astype(np.float64)) if context is not None and len(context) > len(x) else spec
    noise, floor_db = estimate_noise_psd(noise_source)
    info["noise_floor_db"] = round(floor_db, 1)
    if noise is None or floor_db > MAX_USABLE_NOISE_DB:
        info["skipped"] = "no usable noise profile"
        return x, info

    # Over-estimating the noise suppresses more; the floor gain bounds how deep any bin may be cut.
    noise = noise * (1.0 + 1.5 * strength) + 1e-12
    min_gain = 10 ** (-(6.0 + 20.0 * strength) / 20.0)
    power = np.abs(spec) ** 2
    gamma = power / noise[:, None]  # a posteriori SNR
    gain = np.empty_like(gamma)
    prev_gain = np.ones(gamma.shape[0])
    prev_gamma = np.maximum(gamma[:, 0] - 1.0, 0.0)
    for t in range(gamma.shape[1]):
        xi = SMOOTHING * (prev_gain**2) * prev_gamma + (1.0 - SMOOTHING) * np.maximum(gamma[:, t] - 1.0, 0.0)
        g = xi / (1.0 + xi)
        g = np.maximum(g, min_gain)
        gain[:, t] = g
        prev_gain, prev_gamma = g, gamma[:, t]

    # A light smoothing across frequency removes isolated "birdie" bins.
    gain[1:-1] = 0.25 * gain[:-2] + 0.5 * gain[1:-1] + 0.25 * gain[2:]
    out = _sft.istft(spec * gain, k1=len(x)).real.astype(np.float32)
    info.update(applied=True, strength=round(strength, 2), mean_gain_db=round(float(20 * np.log10(gain.mean() + 1e-9)), 1))
    return out, info
