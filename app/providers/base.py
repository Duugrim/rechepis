from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BODY_LIMIT = 4000
TRACE_LINES = 14


@dataclass
class TranscriptResult:
    text: str
    model: str
    elapsed_s: float = 0.0
    # Timed pieces if the provider gives them: [{"start": s, "end": s, "text": str}]
    segments: list[dict[str, Any]] = field(default_factory=list)
    # Whatever should be kept on disk next to the text (provider response, parameters, timings).
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """A failure with enough detail to be shown to the user as is.

    ``stage`` says where it happened (``config`` | ``audio`` | ``model`` | ``transcribe``),
    ``hint`` is a plain-language suggestion, ``body`` the unabridged answer of a remote service.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str = "transcribe",
        http_status: int | None = None,
        body: str | None = None,
        hint: str | None = None,
        retryable: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.http_status = http_status
        self.body = body
        self.hint = hint
        self.retryable = retryable
        self.extra = extra or {}


def _trace(exc: BaseException) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    text = "".join(lines).strip().splitlines()
    return "\n".join(text[-TRACE_LINES:])


def _chain(exc: BaseException) -> list[str]:
    out: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc.__cause__ or exc.__context__
    while cur is not None and id(cur) not in seen and len(out) < 4:
        seen.add(id(cur))
        out.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return out


def describe_error(
    exc: BaseException,
    *,
    provider: str,
    stage: str = "transcribe",
    context: dict[str, Any] | None = None,
    attempts: int = 1,
) -> dict[str, Any]:
    """Turns any exception into the structure stored in segment meta and shown in the UI."""
    info: dict[str, Any] = {
        "provider": provider,
        "stage": stage,
        "type": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
        "http_status": None,
        "body": None,
        "hint": None,
        "retryable": False,
        "attempts": attempts,
        "at": datetime.now(timezone.utc).isoformat(),
        "context": context or {},
        "caused_by": _chain(exc),
        "traceback": _trace(exc),
    }
    if isinstance(exc, ProviderError):
        info.update(
            stage=exc.stage,
            message=exc.message,
            http_status=exc.http_status,
            body=(exc.body or "")[:BODY_LIMIT] or None,
            hint=exc.hint,
            retryable=exc.retryable,
        )
        info["attempts"] = exc.extra.get("attempts", attempts)
        info["context"] = {**info["context"], **{k: v for k, v in exc.extra.items() if k != "attempts"}}
    if info["hint"] is None:
        info["hint"] = _guess_hint(exc, info)
    return info


def _guess_hint(exc: BaseException, info: dict[str, Any]) -> str | None:
    status = info.get("http_status")
    name = info["type"]
    text = f"{info['message']} {' '.join(info['caused_by'])}".lower()
    if status == 413:
        return "Файл слишком большой для сервиса. Включите нарезку на более короткие сегменты."
    if status == 429:
        return "Превышен лимит запросов. Подождите минуту и повторите."
    if status and status >= 500:
        return "Ошибка на стороне сервиса. Обычно помогает повторить позже."
    if any(m in text for m in ("getaddrinfo", "connecterror", "connecttimeout", "network is unreachable", "name or service")):
        return "Нет связи с сервисом. Проверьте интернет или VPN и повторите."
    if "timeout" in name.lower() or "timed out" in text:
        return "Сервис не ответил вовремя. Повторите; если сегмент длинный — нарежьте короче."
    if isinstance(exc, (MemoryError,)) or "out of memory" in text:
        return "Не хватило памяти. Закройте тяжёлые программы или нарежьте сегменты короче."
    if isinstance(exc, ModuleNotFoundError) or "no module named" in text:
        return "Не установлен нужный пакет. Запустите «Install GigaAM.bat» и перезапустите программу."
    return None


class Provider:
    id: str = ""
    label: str = ""
    kind: str = "cloud"  # cloud | local

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    async def transcribe(self, audio_path: Path, *, language: str, duration_s: float) -> TranscriptResult:
        raise NotImplementedError
