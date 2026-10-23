"""Local speech recognition with Sber's GigaAM v3.

The ``gigaam`` package is only used for the model code; loading audio through its ffmpeg helper is
bypassed (we hand it float arrays), and the weights are fetched by our own downloader so the UI can show
progress and the files live in ``models/gigaam`` next to the project.

GigaAM takes at most ~25 s per call, so longer audio is cut at pauses with the same segmenter that cuts
sessions, transcribed piece by piece and joined.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..audio import SAMPLE_RATE
from ..audio.segmenter import cut_audio
from ..audio.wavio import read_wav
from ..config import MODELS_DIR, load_config
from ..settings import GIGAAM_MODELS, normalize_slicing
from .base import Provider, ProviderError, TranscriptResult

log = logging.getLogger("rechepis.gigaam")

URL_DIR = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM"
MAX_CHUNK_S = 24.0
CHUNK_SLICING = normalize_slicing(
    {"min_len_s": 2.0, "target_len_s": 10.0, "max_len_s": MAX_CHUNK_S, "pause_ms": 300, "hard_pause_ms": 1000, "pad_ms": 200}
)

# md5 of the checkpoints, as published by the gigaam package (it refuses a file that does not match).
MODEL_MD5 = {
    "v3_ctc": "73413e7be9c6a5935827bfab5c0dd678",
    "v3_rnnt": "0fd2c9a1ff66abd8d32a3a07f7592815",
    "v3_e2e_ctc": "367074d6498f426d960b25f49531cf68",
    "v3_e2e_rnnt": "2730de7545ac43ad256485a462b0a27a",
}


def model_dir() -> Path:
    return MODELS_DIR / "gigaam"


def model_paths(name: str) -> tuple[Path, Path | None]:
    ckpt = model_dir() / f"{name}.ckpt"
    tokenizer = model_dir() / f"{name}_tokenizer.model" if "e2e" in name else None
    return ckpt, tokenizer


def is_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "torchaudio", "gigaam", "hydra", "omegaconf", "sentencepiece"))


def is_downloaded(name: str) -> bool:
    ckpt, tokenizer = model_paths(name)
    return ckpt.is_file() and (tokenizer is None or tokenizer.is_file())


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class GigaAMProvider(Provider):
    id = "gigaam"
    label = "GigaAM"
    kind = "local"

    def __init__(self) -> None:
        self._model: Any = None
        self._loaded_name: str | None = None
        self._device: str | None = None
        self._state_lock = threading.Lock()
        # One worker: model load and inference never overlap, and CPU cores are not oversubscribed.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gigaam")
        self._download: dict[str, Any] = {"state": "idle", "model": None, "done": 0, "total": 0, "error": None}
        self._loading = False
        self._on_change: Callable[[], None] | None = None

    def set_change_callback(self, cb: Callable[[], None]) -> None:
        self._on_change = cb

    def _changed(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                log.exception("status callback failed")

    # -- status --------------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        cfg = load_config()["gigaam"]
        name = cfg["model"]
        installed = is_installed()
        downloaded = installed and is_downloaded(name)
        download = dict(self._download)
        if not installed:
            state, message = "not_installed", "Пакеты не установлены — запустите Install GigaAM.bat"
        elif download["state"] == "downloading":
            state, message = "downloading", "Скачивание модели…"
        elif not downloaded:
            state, message = "no_model", "Модель не скачана"
        elif self._loading:
            state, message = "loading", "Загрузка модели в память…"
        elif self._model is not None and self._loaded_name == name:
            state, message = "ready", f"Загружена ({self._device})"
        else:
            state, message = "idle", "Готова к работе (загрузится при первом сегменте)"
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "ready": state in ("ready", "idle", "loading"),
            "state": state,
            "message": message,
            "model": name,
            "installed": installed,
            "downloaded": downloaded,
            "loaded": self._model is not None and self._loaded_name == name,
            "device": self._device,
            "download": download,
            "models": {
                key: {**meta, "downloaded": installed and is_downloaded(key)} for key, meta in GIGAAM_MODELS.items()
            },
        }

    # -- download -----------------------------------------------------------------------------------

    def start_download(self, name: str) -> bool:
        if name not in GIGAAM_MODELS:
            raise ProviderError(f"Неизвестная модель {name}", stage="config")
        with self._state_lock:
            if self._download["state"] == "downloading":
                return False
            self._download = {"state": "downloading", "model": name, "done": 0, "total": 0, "error": None}
        threading.Thread(target=self._download_worker, args=(name,), name="gigaam-download", daemon=True).start()
        self._changed()
        return True

    def _download_worker(self, name: str) -> None:
        try:
            ckpt, tokenizer = model_paths(name)
            # Tokenizer first: it is tiny, and the big checkpoint then reports progress of its own.
            targets = ([(f"{name}_tokenizer.model", tokenizer)] if tokenizer else []) + [(f"{name}.ckpt", ckpt)]
            model_dir().mkdir(parents=True, exist_ok=True)
            for fname, path in targets:
                if not path.exists():
                    self._fetch(f"{URL_DIR}/{fname}", path)
            expected = MODEL_MD5.get(name)
            if expected and _md5(ckpt) != expected:
                ckpt.unlink(missing_ok=True)
                raise RuntimeError("Контрольная сумма модели не совпала — файл удалён, скачайте заново")
            self._download.update(state="done", error=None)
        except Exception as exc:  # noqa: BLE001
            log.exception("GigaAM download failed")
            self._download.update(state="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._changed()

    def _fetch(self, url: str, path: Path, attempts: int = 6) -> None:
        """Streams ``url`` into ``path`` via a .part file; resumes after a dropped connection."""
        import httpx

        part = path.with_name(path.name + ".part")
        for attempt in range(1, attempts + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": "Rechepis",**({"Range": f"bytes={have}-"} if have else {})}
            try:
                with httpx.stream("GET", url, headers=headers, timeout=60.0, follow_redirects=True) as resp:
                    if resp.status_code == 416:  # already complete
                        break
                    resp.raise_for_status()
                    resumed = resp.status_code == 206
                    if not resumed:
                        have = 0
                    length = int(resp.headers.get("Content-Length") or 0)
                    self._download["total"] = have + length
                    self._download["done"] = have
                    last_report = 0.0
                    with open(part, "ab" if resumed else "wb") as out:
                        for block in resp.iter_bytes(1 << 18):
                            out.write(block)
                            self._download["done"] += len(block)
                            now = time.monotonic()
                            if now - last_report > 0.4:
                                last_report = now
                                self._changed()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == attempts:
                    raise
                log.warning("download of %s interrupted (%s), retry %d/%d", url, exc, attempt, attempts)
                time.sleep(2.0 * attempt)
        part.replace(path)

    # -- model ---------------------------------------------------------------------------------------

    def _load_blocking(self) -> Any:
        cfg = load_config()["gigaam"]
        name = cfg["model"]
        if self._model is not None and self._loaded_name == name:
            return self._model
        if not is_installed():
            raise ProviderError(
                "Пакеты GigaAM не установлены",
                stage="model",
                hint="Запустите «Install GigaAM.bat» в папке программы и перезапустите Речепись.",
            )
        if not is_downloaded(name):
            raise ProviderError(
                f"Модель {name} не скачана", stage="model", hint="Откройте настройки → Распознавание → GigaAM и нажмите «Скачать»."
            )
        self._loading = True
        self._changed()
        try:
            import torch
            import gigaam

            if cfg["threads"]:
                torch.set_num_threads(int(cfg["threads"]))
            device = cfg["device"]
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cuda" and not torch.cuda.is_available():
                raise ProviderError("CUDA недоступна", stage="model", hint="Выберите устройство «auto» или «cpu» в настройках.")
            started = time.monotonic()
            model = gigaam.load_model(name, device=device, download_root=str(model_dir()))
            log.info("GigaAM %s loaded on %s in %.1fs", name, device, time.monotonic() - started)
            self._model, self._loaded_name, self._device = model, name, device
            return model
        finally:
            self._loading = False
            self._changed()

    def unload(self) -> None:
        self._model, self._loaded_name, self._device = None, None, None
        self._changed()

    def warm_up(self) -> None:
        """Load the model in the background so the first segment does not wait for it."""
        self._pool.submit(self._safe_load)

    def _safe_load(self) -> None:
        try:
            self._load_blocking()
        except Exception:  # noqa: BLE001
            log.exception("GigaAM warm-up failed")

    # -- inference -----------------------------------------------------------------------------------

    def _infer_chunk(self, model: Any, x: np.ndarray) -> str:
        import torch

        with torch.inference_mode():
            wav = torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))[None, :]
            wav = wav.to(model._device).to(model._dtype)
            length = torch.full([1], wav.shape[-1], device=model._device)
            encoded, encoded_len = model.forward(wav, length)
            decoded = model._decode(encoded, encoded_len, length, False)
        return (decoded[0][0] or "").strip()

    def transcribe_samples(self, x: np.ndarray) -> tuple[str, list[dict[str, Any]]]:
        """Blocking. Returns the text and the pieces it was assembled from."""
        model = self._load_blocking()
        if len(x) == 0:
            return "", []
        if len(x) <= MAX_CHUNK_S * SAMPLE_RATE:
            return self._infer_chunk(model, x), []
        pieces: list[dict[str, Any]] = []
        for cut in cut_audio(x, CHUNK_SLICING):
            text = self._infer_chunk(model, x[cut.start : cut.end])
            if text:
                pieces.append({"start": round(cut.start / SAMPLE_RATE, 3), "end": round(cut.end / SAMPLE_RATE, 3), "text": text})
        return " ".join(p["text"] for p in pieces), pieces

    def _transcribe_file_blocking(self, audio_path: Path, duration_s: float) -> TranscriptResult:
        started = time.monotonic()
        try:
            x, rate = read_wav(audio_path)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Не удалось прочитать аудио: {exc}", stage="audio") from exc
        if rate != SAMPLE_RATE:
            from ..audio.resample import resample

            x = resample(x, rate, SAMPLE_RATE)
        text, pieces = self.transcribe_samples(x)
        name = self._loaded_name or load_config()["gigaam"]["model"]
        return TranscriptResult(
            text=text,
            model=name,
            elapsed_s=time.monotonic() - started,
            segments=pieces,
            raw={"model": name, "device": self._device, "audio_s": round(len(x) / SAMPLE_RATE, 3), "chunks": len(pieces) or 1},
        )

    async def transcribe(self, audio_path: Path, *, language: str, duration_s: float) -> TranscriptResult:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, self._transcribe_file_blocking, audio_path, duration_s)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            stage = "model" if "load" in type(exc).__name__.lower() or isinstance(exc, (ImportError, OSError)) else "transcribe"
            raise ProviderError(f"{type(exc).__name__}: {exc}", stage=stage) from exc
