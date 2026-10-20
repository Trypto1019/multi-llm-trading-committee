"""Canned-response provider so the demo and tests run with no keys and no network.

Responses are looked up by `agent|model|key`, where `agent` and `key` (usually
a ticker or case id) come from call metadata. Wildcards `*` are tried in order:

    agent|model|key  ->  agent|model|*  ->  agent|*|key  ->  agent|*|*

A canned value may be:
- a dict           -> returned as JSON text
- a str            -> returned verbatim (use it to test JSON extraction)
- {"__error__": m} -> raises ProviderError(m) (use it to test failure handling)
- a list           -> one item per successive call, last item repeats (retry tests)
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from providers.base import Completion, Message, ProviderError


class MockProvider:
    name = "mock"

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[dict[str, Any]] = []
        self._counters: defaultdict[str, int] = defaultdict(int)

    @classmethod
    def from_file(cls, path: str | Path) -> "MockProvider":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data.pop("_comment", None)
        return cls(data)

    def _resolve(self, agent: str, model: str, key: str) -> tuple[str, Any]:
        for candidate in (f"{agent}|{model}|{key}", f"{agent}|{model}|*", f"{agent}|*|{key}", f"{agent}|*|*"):
            if candidate in self.responses:
                return candidate, self.responses[candidate]
        raise ProviderError(f"mock: no canned response for {agent}|{model}|{key}")

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
        meta = metadata or {}
        agent, key = str(meta.get("agent", "*")), str(meta.get("key", "*"))
        self.calls.append({"agent": agent, "model": model, "key": key, "messages": list(messages)})
        slot, value = self._resolve(agent, model, key)

        if isinstance(value, list):
            idx = min(self._counters[slot], len(value) - 1)
            self._counters[slot] += 1
            value = value[idx]

        if isinstance(value, dict) and "__error__" in value:
            raise ProviderError(str(value["__error__"]))
        text = value if isinstance(value, str) else json.dumps(value)
        return Completion(text=text, model=model, usage={"total_tokens": len(text) // 4})
