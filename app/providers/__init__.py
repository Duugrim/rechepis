from __future__ import annotations

from .base import Provider, ProviderError, TranscriptResult, describe_error
from .gigaam import GigaAMProvider

_providers: dict[str, Provider] = {}


def get_providers() -> dict[str, Provider]:
    if not _providers:
        for provider in (GigaAMProvider(),):
            _providers[provider.id] = provider
    return _providers


def get_provider(provider_id: str) -> Provider:
    try:
        return get_providers()[provider_id]
    except KeyError:
        raise ProviderError(f"Неизвестный провайдер: {provider_id}", stage="config") from None


__all__ = ["Provider", "ProviderError", "TranscriptResult", "describe_error", "get_provider", "get_providers"]
