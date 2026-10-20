"""Schema-validated agent base class.

Every model in the committee is wrapped in an agent that:
- loads its role prompt from `prompts/<prompt_name>.md` (cached, hot-reloadable)
- routes the call through a `Provider` (OpenRouter-compatible HTTP or mock)
- extracts JSON robustly from whatever the model actually returned
- validates it against a JSON schema
- retries with targeted feedback on parse/validation failure, with backoff on
  transport failure
- raises a *typed* error when it finally gives up, so callers can decide
  whether a failure is an abstention, a veto, or a crash

Keys never pass through here; providers read them from the environment.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.schema import SchemaError, validate
from providers.base import Message, Provider, ProviderError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


# --------------------------------------------------------------------- errors
class AgentError(Exception):
    """Base class for every agent failure."""


class AgentParseError(AgentError):
    """The model response did not contain a parseable JSON object."""


class AgentValidationError(AgentError):
    """The JSON parsed but violated the agent's output schema."""


class AgentCallError(AgentError):
    """The provider call itself failed (network, HTTP, timeout, missing key)."""


# -------------------------------------------------------------- JSON extraction
_DECODER = json.JSONDecoder()


def extract_json(content: str) -> dict[str, Any]:
    """Pull one JSON object out of a model response.

    Order: whole string -> fenced ```json block -> first decodable `{...}`
    anywhere in the text (scanning each opening brace with raw_decode, so
    trailing prose or a second object does not break it).
    """
    text = (content or "").strip()
    if not text:
        raise AgentParseError("empty response")

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    fence_start = text.find("```")
    while fence_start != -1:
        body_start = text.find("\n", fence_start)
        fence_end = text.find("```", fence_start + 3)
        if body_start == -1 or fence_end == -1:
            break
        block = text[body_start + 1:fence_end].strip()
        try:
            value = json.loads(block)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        fence_start = text.find("```", fence_end + 3)

    idx = text.find("{")
    while idx != -1:
        try:
            value, _ = _DECODER.raw_decode(text, idx)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)

    raise AgentParseError(f"no JSON object found (response starts: {text[:80]!r})")


# --------------------------------------------------------------------- logging
@dataclass(frozen=True)
class CallRecord:
    call_id: str
    agent: str
    model: str
    key: str | None
    success: bool
    attempts: int
    latency_ms: int
    error_type: str | None = None
    error: str | None = None


CallSink = Callable[[CallRecord], None]


# ----------------------------------------------------------------------- agent
class BaseAgent(ABC):
    """Abstract agent. Subclasses define name, model, schema and messages."""

    agent_name: str = "base"
    prompt_name: str | None = None  # defaults to agent_name
    model_id: str = "openai/gpt-4o"
    temperature: float = 0.3
    max_tokens: int = 1500
    timeout: float = 120.0
    max_retries: int = 2
    retry_delay: float = 1.0

    def __init__(
        self,
        provider: Provider,
        *,
        model_id: str | None = None,
        call_sink: CallSink | None = None,
        retry_delay: float | None = None,
        prompts_dir: Path = PROMPTS_DIR,
    ) -> None:
        self.provider = provider
        if model_id:
            self.model_id = model_id
        if retry_delay is not None:
            self.retry_delay = retry_delay
        self.call_sink = call_sink
        self.prompts_dir = prompts_dir
        self._prompt: str | None = None

    # -------------------------------------------------------------- prompts
    @property
    def prompt_path(self) -> Path:
        return self.prompts_dir / f"{self.prompt_name or self.agent_name}.md"

    def load_prompt(self) -> str:
        if self._prompt is None:
            if not self.prompt_path.exists():
                raise AgentError(f"prompt file not found: {self.prompt_path.name}")
            self._prompt = self.prompt_path.read_text(encoding="utf-8")
        return self._prompt

    def reload_prompt(self) -> str:
        self._prompt = None
        return self.load_prompt()

    # ------------------------------------------------------------- contract
    @property
    @abstractmethod
    def output_schema(self) -> dict[str, Any]:
        """JSON schema the parsed output must satisfy."""

    @abstractmethod
    def build_messages(self, **kwargs: Any) -> list[Message]:
        """Build the chat messages for this call."""

    def metadata_key(self, **kwargs: Any) -> str | None:
        """A routing/logging key (ticker, case id). Never sent to real providers."""
        value = kwargs.get("key") or kwargs.get("ticker")
        return str(value) if value is not None else None

    def validate_output(self, data: dict[str, Any]) -> None:
        try:
            validate(data, self.output_schema)
        except SchemaError as e:
            raise AgentValidationError(str(e)) from e

    # ----------------------------------------------------------------- call
    async def call(self, **kwargs: Any) -> dict[str, Any]:
        """Call the model and return parsed, schema-valid JSON or raise a typed AgentError."""
        call_id = uuid.uuid4().hex[:8]
        key = self.metadata_key(**kwargs)
        messages = self.build_messages(**kwargs)
        started = time.monotonic()
        last_error: AgentError | None = None

        for attempt in range(self.max_retries + 1):
            raw: str | None = None
            try:
                completion = await self.provider.complete(
                    self.model_id,
                    messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    metadata={"agent": self.agent_name, "key": key, "attempt": attempt},
                )
                raw = completion.text
                parsed = extract_json(raw)
                self.validate_output(parsed)
                self._log(call_id, key, True, attempt + 1, started)
                return parsed
            except AgentParseError as e:
                last_error = e
                feedback = "Your response was not valid JSON. Respond with ONLY one valid JSON object."
            except AgentValidationError as e:
                last_error = e
                feedback = f"Your JSON failed validation: {e}. Fix it and respond with ONLY the JSON object."
            except (ProviderError, asyncio.TimeoutError, OSError) as e:
                last_error = AgentCallError(f"{self.agent_name}/{self.model_id}: {e}")
                feedback = None

            if attempt >= self.max_retries:
                break
            if feedback is not None:
                messages = [*messages, {"role": "assistant", "content": raw or ""},
                            {"role": "user", "content": feedback}]
                await asyncio.sleep(self.retry_delay)
            else:
                await asyncio.sleep(self.retry_delay * (attempt + 1))

        assert last_error is not None
        self._log(call_id, key, False, self.max_retries + 1, started, last_error)
        raise last_error

    def _log(self, call_id: str, key: str | None, ok: bool, attempts: int, started: float,
             error: AgentError | None = None) -> None:
        if self.call_sink is None:
            return
        self.call_sink(CallRecord(
            call_id=call_id, agent=self.agent_name, model=self.model_id, key=key, success=ok,
            attempts=attempts, latency_ms=int((time.monotonic() - started) * 1000),
            error_type=type(error).__name__ if error else None, error=str(error) if error else None,
        ))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} agent={self.agent_name} model={self.model_id}>"


class JsonRoleAgent(BaseAgent):
    """Concrete agent configured by data: role prompt + schema + JSON payload.

    The system message is the role prompt; the user message is the JSON
    payload plus a reminder of the exact output contract.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        agent_name: str,
        schema: dict[str, Any],
        model_id: str | None = None,
        prompt_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.agent_name = agent_name
        self.prompt_name = prompt_name or agent_name
        self._schema = schema
        super().__init__(provider, model_id=model_id, **kwargs)

    @property
    def output_schema(self) -> dict[str, Any]:
        return self._schema

    def build_messages(self, *, payload: dict[str, Any], **_: Any) -> list[Message]:
        return [
            {"role": "system", "content": self.load_prompt()},
            {"role": "user", "content": (
                "INPUT:\n" + json.dumps(payload, indent=2, sort_keys=True, default=str)
                + "\n\nRespond with ONLY one JSON object matching this schema:\n"
                + json.dumps(self._schema, sort_keys=True)
            )},
        ]
