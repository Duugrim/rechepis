"""Defaults, presets and validation for everything the user can tune.

Three groups of settings travel together through the app:

* ``slicing``    — how a session is cut into segments;
* ``processing`` — how each segment is cleaned and levelled before it is sent to a model;
* ``config``     — recognition model, default targets, microphone.

Values coming from the UI (or a hand-edited config.json) are always passed through the ``normalize_*``
functions, so the audio code can trust them.
"""

from __future__ import annotations

from typing import Any

PROVIDER_IDS = ("gigaam",)

DEFAULT_SLICING: dict[str, Any] = {
    "enabled": True,
    "min_len_s": 3.0,
    "target_len_s": 12.0,
    "max_len_s": 24.0,
    "pause_ms": 450,
    "hard_pause_ms": 1400,
    "pad_ms": 250,
    "sensitivity_db": 10.0,
    "pause_depth_db": 20.0,
}

# name -> (label, partial overrides over DEFAULT_SLICING)
SLICING_PRESETS: dict[str, tuple[str, dict[str, Any]]] = {
    "balanced": ("Сбалансированная", {}),
    "short": ("Короткие фразы", {"min_len_s": 2.0, "target_len_s": 6.0, "max_len_s": 15.0, "pause_ms": 350}),
    "long": ("Длинные куски", {"min_len_s": 5.0, "target_len_s": 20.0, "max_len_s": 24.0, "pause_ms": 600}),
    "off": ("Не резать (один сегмент)", {"enabled": False}),
}

DEFAULT_PROCESSING: dict[str, Any] = {
    "highpass_hz": 80,
    "denoise": 0.5,
    "normalize": True,
    "target_dbfs": -20.0,
    "max_gain_db": 30.0,
}

PROCESSING_PRESETS: dict[str, tuple[str, dict[str, Any]]] = {
    "raw": ("Без обработки", {"highpass_hz": 0, "denoise": 0.0, "normalize": False}),
    "light": ("Только громкость", {"highpass_hz": 80, "denoise": 0.0, "normalize": True}),
    "standard": ("Стандартная", {}),
    "strong": ("Сильная очистка", {"highpass_hz": 100, "denoise": 0.85}),
}

DEFAULT_GIGAAM: dict[str, Any] = {
    "model": "v3_e2e_rnnt",
    "device": "auto",
    "threads": 0,
}

GIGAAM_MODELS: dict[str, dict[str, Any]] = {
    "v3_e2e_rnnt": {
        "label": "v3 e2e RNN-T",
        "note": "с пунктуацией и нормализацией чисел; точнее e2e CTC",
        "punctuation": True,
        "size_mb": 428,
    },
    "v3_e2e_ctc": {
        "label": "v3 e2e CTC",
        "note": "с пунктуацией; быстрее e2e RNN-T",
        "punctuation": True,
        "size_mb": 422,
    },
    "v3_rnnt": {
        "label": "v3 RNN-T",
        "note": "без пунктуации, минимум ошибок в словах",
        "punctuation": False,
        "size_mb": 426,
    },
    "v3_ctc": {
        "label": "v3 CTC",
        "note": "без пунктуации, самая быстрая",
        "punctuation": False,
        "size_mb": 421,
    },
}

DEFAULT_MIC: dict[str, Any] = {
    "device_id": "",
    "echo_cancellation": False,
    "noise_suppression": False,
    "auto_gain": False,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "language": "ru",
    "gigaam": DEFAULT_GIGAAM,
    "targets": ["gigaam"],
    "paragraph_gap_s": 1.5,
    "slicing": DEFAULT_SLICING,
    "slicing_preset": "balanced",
    "processing": DEFAULT_PROCESSING,
    "processing_preset": "standard",
    "mic": DEFAULT_MIC,
}


def _num(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def normalize_slicing(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = {**DEFAULT_SLICING, **(raw or {})}
    max_len = _num(src["max_len_s"], DEFAULT_SLICING["max_len_s"], 5.0, 300.0)
    target = _num(src["target_len_s"], DEFAULT_SLICING["target_len_s"], 2.0, max_len)
    min_len = _num(src["min_len_s"], DEFAULT_SLICING["min_len_s"], 0.5, target)
    pause = _num(src["pause_ms"], DEFAULT_SLICING["pause_ms"], 100, 3000)
    return {
        "enabled": _flag(src["enabled"], True),
        "min_len_s": round(min_len, 2),
        "target_len_s": round(target, 2),
        "max_len_s": round(max_len, 2),
        "pause_ms": int(pause),
        "hard_pause_ms": int(_num(src["hard_pause_ms"], DEFAULT_SLICING["hard_pause_ms"], pause, 10000)),
        "pad_ms": int(_num(src["pad_ms"], DEFAULT_SLICING["pad_ms"], 0, 1000)),
        "sensitivity_db": round(_num(src["sensitivity_db"], DEFAULT_SLICING["sensitivity_db"], 3.0, 30.0), 1),
        "pause_depth_db": round(_num(src["pause_depth_db"], DEFAULT_SLICING["pause_depth_db"], 6.0, 40.0), 1),
    }


def normalize_processing(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = {**DEFAULT_PROCESSING, **(raw or {})}
    return {
        "highpass_hz": int(_num(src["highpass_hz"], DEFAULT_PROCESSING["highpass_hz"], 0, 400)),
        "denoise": round(_num(src["denoise"], DEFAULT_PROCESSING["denoise"], 0.0, 1.0), 2),
        "normalize": _flag(src["normalize"], True),
        "target_dbfs": round(_num(src["target_dbfs"], DEFAULT_PROCESSING["target_dbfs"], -40.0, -6.0), 1),
        "max_gain_db": round(_num(src["max_gain_db"], DEFAULT_PROCESSING["max_gain_db"], 0.0, 60.0), 1),
    }


def processing_from_preset(name: str) -> dict[str, Any]:
    label_and_params = PROCESSING_PRESETS.get(name)
    if label_and_params is None:
        raise ValueError(f"Неизвестный пресет обработки: {name}")
    return normalize_processing(label_and_params[1])


def slicing_from_preset(name: str) -> dict[str, Any]:
    label_and_params = SLICING_PRESETS.get(name)
    if label_and_params is None:
        raise ValueError(f"Неизвестный пресет нарезки: {name}")
    return normalize_slicing(label_and_params[1])


def processing_label(params: dict[str, Any]) -> str:
    """Name of the preset these parameters equal, or ``custom``."""
    clean = normalize_processing(params)
    for name in PROCESSING_PRESETS:
        if processing_from_preset(name) == clean:
            return name
    return "custom"


def slicing_label(params: dict[str, Any]) -> str:
    clean = normalize_slicing(params)
    for name in SLICING_PRESETS:
        if slicing_from_preset(name) == clean:
            return name
    return "custom"


def is_raw(params: dict[str, Any]) -> bool:
    clean = normalize_processing(params)
    return clean["highpass_hz"] == 0 and clean["denoise"] == 0 and not clean["normalize"]


def normalize_targets(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    seen: list[str] = []
    for item in raw:
        if item in PROVIDER_IDS and item not in seen:
            seen.append(item)
    return seen


def normalize_gigaam(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = {**DEFAULT_GIGAAM, **(raw or {})}
    model = str(src["model"])
    if model not in GIGAAM_MODELS:
        model = DEFAULT_GIGAAM["model"]
    device = str(src["device"]).lower()
    if device not in ("auto", "cpu", "cuda"):
        device = "auto"
    return {"model": model, "device": device, "threads": int(_num(src["threads"], 0, 0, 64))}


def normalize_mic(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = {**DEFAULT_MIC, **(raw or {})}
    return {
        "device_id": str(src["device_id"] or ""),
        "echo_cancellation": _flag(src["echo_cancellation"], False),
        "noise_suppression": _flag(src["noise_suppression"], False),
        "auto_gain": _flag(src["auto_gain"], False),
    }


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Full, valid config from whatever is on disk (unknown keys are dropped)."""
    src = raw or {}
    return {
        "language": (str(src.get("language") or "ru").strip() or "ru"),
        "gigaam": normalize_gigaam(src.get("gigaam")),
        "targets": normalize_targets(src.get("targets", DEFAULT_CONFIG["targets"])),
        "paragraph_gap_s": round(_num(src.get("paragraph_gap_s"), DEFAULT_CONFIG["paragraph_gap_s"], 0.3, 30.0), 2),
        "slicing": normalize_slicing(src.get("slicing")),
        "slicing_preset": str(src.get("slicing_preset") or "balanced"),
        "processing": normalize_processing(src.get("processing")),
        "processing_preset": str(src.get("processing_preset") or "standard"),
        "mic": normalize_mic(src.get("mic")),
    }
