from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf

from . import SAMPLE_RATE


def wav_frames(path: Path) -> tuple[int, int]:
    """(frames, sample_rate) of a wav file without reading the audio."""
    info = sf.info(str(path))
    return int(info.frames), int(info.samplerate)


def read_wav(path: Path, start: int = 0, frames: int = -1) -> tuple[np.ndarray, int]:
    """Mono float32 samples ``[start, start+frames)`` and the file's sample rate."""
    data, rate = sf.read(str(path), start=int(start), frames=int(frames), dtype="float32", always_2d=True)
    return (data[:, 0] if data.shape[1] == 1 else data.mean(axis=1)).astype(np.float32, copy=False), int(rate)


def write_wav(path: Path, data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    sf.write(str(tmp), np.clip(data, -1.0, 1.0), int(sample_rate), subtype="PCM_16", format="WAV")
    os.replace(tmp, path)


def read_pcm(path: Path, start: int, count: int) -> np.ndarray:
    """Slice of a raw little-endian int16 mono file as float32 in [-1, 1)."""
    raw = np.fromfile(path, dtype="<i2", count=int(count), offset=int(start) * 2)
    return raw.astype(np.float32) / 32768.0


def pcm_to_wav(pcm_path: Path, wav_path: Path, sample_rate: int = SAMPLE_RATE, block: int = 1 << 20) -> int:
    """Wrap a raw int16 mono file into a wav (streamed, so long sessions do not load into memory)."""
    tmp = wav_path.with_name(wav_path.name + ".tmp")
    total = 0
    with sf.SoundFile(str(tmp), mode="w", samplerate=int(sample_rate), channels=1, subtype="PCM_16", format="WAV") as out:
        with open(pcm_path, "rb") as src:
            while True:
                chunk = src.read(block * 2)
                if not chunk:
                    break
                chunk = chunk[: len(chunk) - (len(chunk) % 2)]
                out.write(np.frombuffer(chunk, dtype="<i2"))
                total += len(chunk) // 2
    os.replace(tmp, wav_path)
    return total


def peaks(path: Path, buckets: int) -> list[list[float]]:
    """Min/max envelope of a wav in ``buckets`` slices, read block by block."""
    frames, _ = wav_frames(path)
    if frames == 0 or buckets <= 0:
        return []
    buckets = min(buckets, frames)
    edges = np.linspace(0, frames, buckets + 1).astype(np.int64)
    out: list[list[float]] = []
    with sf.SoundFile(str(path)) as f:
        for i in range(buckets):
            f.seek(int(edges[i]))
            block = f.read(int(edges[i + 1] - edges[i]), dtype="float32", always_2d=True)
            if block.size == 0:
                out.append([0.0, 0.0])
                continue
            mono = block[:, 0]
            out.append([round(float(mono.min()), 4), round(float(mono.max()), 4)])
    return out
