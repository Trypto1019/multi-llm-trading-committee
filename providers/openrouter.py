"""OpenRouter-compatible chat-completions provider (stdlib only).

Any endpoint that speaks the OpenAI chat-completions wire format works:
set LLM_BASE_URL to point elsewhere. The API key is read from the environment
at call time and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from providers.base import Completion, Message, ProviderError

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, base_url: str | None = None, api_key_env: str = "OPENROUTER_API_KEY") -> None:
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env = api_key_env

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ProviderError(f"{self.api_key_env} is not set; use --mock to run offline")
        return key

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https base URL
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise ProviderError(f"HTTP {e.code} from provider") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise ProviderError(f"provider transport error: {type(e).__name__}") from e

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
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        body = await asyncio.to_thread(self._post, payload, timeout)
        try:
            text = body["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"{model}: unexpected response envelope") from e
        return Completion(text=text.strip(), model=model, usage=dict(body.get("usage") or {}))
