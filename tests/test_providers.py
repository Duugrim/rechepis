import asyncio

import httpx
import pytest

from app.providers.base import ProviderError, describe_error
from app.providers.gigaam import GigaAMProvider


@pytest.fixture()
def wav(tmp_path):
    path = tmp_path / "a.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


def test_gigaam_without_the_model_gives_an_actionable_error(wav):
    provider = GigaAMProvider()
    status = provider.status()
    assert status["downloaded"] is False and status["state"] in ("no_model", "not_installed")
    assert not status["ready"]
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.transcribe(wav, language="ru", duration_s=1))
    assert caught.value.stage in ("model", "audio")


def test_describe_error_handles_plain_exceptions_and_nested_causes():
    try:
        try:
            raise OSError("disk full")
        except OSError as inner:
            raise RuntimeError("could not save") from inner
    except RuntimeError as exc:
        info = describe_error(exc, provider="gigaam", stage="audio", attempts=2)
    assert info["type"] == "RuntimeError" and info["attempts"] == 2
    assert info["caused_by"] == ["OSError: disk full"]
    assert "RuntimeError" in info["traceback"]


def test_describe_error_gives_a_hint_for_network_and_server_failures():
    offline = describe_error(httpx.ConnectError("getaddrinfo failed"), provider="gigaam", stage="model")
    assert "интернет" in offline["hint"]
    down = describe_error(ProviderError("boom", stage="model", http_status=503, body="down"), provider="gigaam")
    assert down["http_status"] == 503 and down["body"] == "down" and "позже" in down["hint"]
