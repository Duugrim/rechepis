"""The processing chain a segment goes through before it is sent to a recogniser."""

from __future__ import annotations

import numpy as np

from ..settings import is_raw, normalize_processing
from .denoise import denoise
from .enhance import analyze, highpass, normalize

# Quality warnings shown next to a segment: key -> (condition on stats of the *original* audio, text)
QUIET_SPEECH_DBFS = -38.0
LOW_SNR_DB = 12.0
CLIPPING_RATIO = 0.002


def warnings_for(stats: dict) -> list[str]:
    out: list[str] = []
    if stats.get("speech_rms_dbfs") is None:
        out.append("no_speech")
        return out
    if stats["speech_rms_dbfs"] < QUIET_SPEECH_DBFS:
        out.append("quiet")
    if stats["clipped_ratio"] > CLIPPING_RATIO:
        out.append("clipping")
    if stats.get("snr_db") is not None and stats["snr_db"] < LOW_SNR_DB:
        out.append("noisy")
    return out


def process_audio(raw: np.ndarray, params: dict, context: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """Returns the processed audio and a report: what was measured and what was done.

    ``context`` is more audio from the same recording (before and around the segment); it only serves to
    learn what the room sounds like.
    """
    params = normalize_processing(params)
    report: dict = {"params": params, "original": analyze(raw)}
    report["warnings"] = warnings_for(report["original"])
    if is_raw(params) or len(raw) == 0:
        report["steps"] = []
        return raw.astype(np.float32, copy=False), report

    steps: list[dict] = []
    x = raw.astype(np.float32)
    if params["highpass_hz"]:
        x = highpass(x, params["highpass_hz"])
        steps.append({"step": "highpass", "hz": params["highpass_hz"]})
    if params["denoise"] > 0:
        ctx = highpass(context, params["highpass_hz"]) if context is not None and params["highpass_hz"] else context
        x, info = denoise(x, params["denoise"], ctx)
        steps.append({"step": "denoise", **info})
    if params["normalize"]:
        level = analyze(x)["speech_rms_dbfs"]
        x, gain_db = normalize(x, params["target_dbfs"], params["max_gain_db"], level)
        steps.append({"step": "normalize", "gain_db": round(gain_db, 1), "target_dbfs": params["target_dbfs"]})
    report["steps"] = steps
    report["processed"] = analyze(x)
    return np.clip(x, -1.0, 1.0), report
