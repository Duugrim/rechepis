from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .settings import DEFAULT_CONFIG, PROVIDER_IDS, normalize_config

ROOT_DIR = Path(__file__).resolve().parent.parent
# RECHEPIS_DATA moves recordings, models and the config elsewhere, so test runs never touch real work.
DATA_DIR = Path(os.environ.get("RECHEPIS_DATA") or ROOT_DIR)
CONFIG_PATH = DATA_DIR / "config.json"
RECORDINGS_DIR = DATA_DIR / "recordings"
MODELS_DIR = DATA_DIR / "models"
WEB_DIR = ROOT_DIR / "web"

_config_lock = threading.RLock()


def ensure_dirs() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _read_file() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_config() -> dict[str, Any]:
    with _config_lock:
        return normalize_config(_read_file())


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored config. Nested groups (slicing, processing, ...) merge key by key."""
    with _config_lock:
        current = normalize_config(_read_file())
        for key, value in updates.items():
            if key not in DEFAULT_CONFIG or value is None:
                continue
            if isinstance(DEFAULT_CONFIG[key], dict) and isinstance(value, dict):
                current[key] = {**current[key], **value}
            else:
                current[key] = value
        clean = normalize_config(current)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    return load_config()


def public_config() -> dict[str, Any]:
    """Config as the UI sees it, plus where the data lives."""
    data = load_config()
    data["recordings_dir"] = str(RECORDINGS_DIR)
    data["models_dir"] = str(MODELS_DIR)
    data["provider_ids"] = list(PROVIDER_IDS)
    return data
