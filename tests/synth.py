"""Speech-like test signals: no real voice needed to exercise the detector and the cutter."""

from __future__ import annotations

import numpy as np

SR = 16000


def speech(seconds: float, level: float = 0.1, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 3.5 * t) ** 2  # syllable rhythm, never silent
    sig = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) for f in (140, 280, 420, 1200, 2400)) * envelope
    return (sig / np.abs(sig).max() * level).astype(np.float32)


def room(seconds: float, level: float = 0.002, seed: int = 1) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * level).astype(np.float32)


def dictation(parts: list[tuple[str, float]]) -> np.ndarray:
    """``[("room", 1.0), ("speech", 5.0), ...]`` -> one signal."""
    chunks = []
    for i, (kind, seconds) in enumerate(parts):
        chunks.append(speech(seconds, seed=i) if kind == "speech" else room(seconds, seed=100 + i))
    return np.concatenate(chunks)


def pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
