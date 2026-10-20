"""Model routing configuration. Model ids are OpenRouter-style `vendor/model` strings.

In mock mode the defaults are always used, because the canned responses are
keyed by these ids. With a real provider each id can be overridden from env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_COUNCIL = ("openai/gpt-4o", "google/gemini-2.0-flash-001", "x-ai/grok-3")
DEFAULT_ARBITER = "google/gemini-2.5-pro"
DEFAULT_NEWS = "x-ai/grok-3"
DEFAULT_MACRO = "google/gemini-2.0-flash-001"


@dataclass(frozen=True)
class CommitteeConfig:
    council_models: tuple[str, ...] = DEFAULT_COUNCIL
    arbiter_model: str = DEFAULT_ARBITER
    news_model: str = DEFAULT_NEWS
    macro_model: str = DEFAULT_MACRO
    arbiter_fail_closed: bool = True
    retry_delay: float = 1.0

    @classmethod
    def load(cls, mock: bool) -> "CommitteeConfig":
        if mock:
            return cls(retry_delay=0.0)
        council = tuple(m.strip() for m in os.environ.get("COUNCIL_MODELS", "").split(",") if m.strip())
        return cls(
            council_models=council or DEFAULT_COUNCIL,
            arbiter_model=os.environ.get("ARBITER_MODEL", DEFAULT_ARBITER),
            news_model=os.environ.get("NEWS_MODEL", DEFAULT_NEWS),
            macro_model=os.environ.get("MACRO_MODEL", DEFAULT_MACRO),
        )
