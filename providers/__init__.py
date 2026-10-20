"""LLM provider routing: an OpenRouter-compatible HTTP client or a canned mock."""

from __future__ import annotations

import os
from pathlib import Path

from providers.base import Completion, Message, Provider, ProviderError
from providers.mock import MockProvider
from providers.openrouter import OpenRouterProvider

DEFAULT_MOCK_RESPONSES = Path(__file__).resolve().parent.parent / "data" / "fake_market" / "mock_responses.json"


def get_provider(mock: bool, mock_responses: str | Path = DEFAULT_MOCK_RESPONSES) -> Provider:
    """Return the mock provider when asked. A missing key is an error, never a silent fallback."""
    if mock:
        return MockProvider.from_file(mock_responses)
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ProviderError("OPENROUTER_API_KEY is not set; run with --mock for an offline demo")
    return OpenRouterProvider()


__all__ = [
    "Completion",
    "DEFAULT_MOCK_RESPONSES",
    "Message",
    "MockProvider",
    "OpenRouterProvider",
    "Provider",
    "ProviderError",
    "get_provider",
]
