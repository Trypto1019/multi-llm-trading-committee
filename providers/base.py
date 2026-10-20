"""Provider protocol: anything that turns chat messages into text."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

Message = dict[str, str]


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class ProviderError(RuntimeError):
    """Transport-level failure (network, HTTP status, malformed envelope)."""


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1500,
        timeout: float = 120.0,
        metadata: dict[str, Any] | None = None,
    ) -> Completion:
        """Return the assistant text for `messages`.

        `metadata` (agent name, ticker, case id, ...) is never sent upstream by
        real providers; the mock provider uses it to pick a canned response.
        """
        ...
