from __future__ import annotations

import asyncio
import json

import pytest

from agents import (
    AgentCallError,
    AgentError,
    AgentParseError,
    AgentValidationError,
    JsonRoleAgent,
    extract_json,
)
from agents.schema import SchemaError, validate
from providers import MockProvider, OpenRouterProvider, ProviderError, get_provider

SCHEMA = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer", "minimum": 0}}}


def agent(responses, **kw):
    calls = []
    a = JsonRoleAgent(MockProvider(responses), agent_name="news_analyst", schema=SCHEMA, model_id="m",
                      retry_delay=0, call_sink=calls.append, **kw)
    return a, calls


# ----------------------------------------------------------- extraction
@pytest.mark.parametrize("text", [
    '{"x": 1}',
    '  {"x": 1}  ',
    '```json\n{"x": 1}\n```',
    '```\n{"x": 1}\n```',
    'Sure! Here you go:\n```json\n{"x": 1}\n```\nAnything else?',
    'Result: {"x": 1} -- hope that helps {not json}',
    'prefix {broken {"x": 1}',
])
def test_extract_json_variants(text):
    assert extract_json(text) == {"x": 1}


def test_extract_json_handles_nested_braces_in_strings():
    assert extract_json('note {"x": 1, "s": "a } b"} tail') == {"x": 1, "s": "a } b"}


@pytest.mark.parametrize("text", ["", "   ", "no json here", "[1, 2, 3]", "{'x': 1}"])
def test_extract_json_failures(text):
    with pytest.raises(AgentParseError):
        extract_json(text)


# ----------------------------------------------------------- schema
def test_schema_validator():
    s = {"type": "object", "required": ["a"], "additionalProperties": False,
         "properties": {"a": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"enum": [1, 2]}},
                        "b": {"type": ["string", "null"], "minLength": 2}}}
    validate({"a": [1, 2], "b": None}, s)
    for bad in ({}, {"a": []}, {"a": [1, 2, 1]}, {"a": [3]}, {"a": [1], "b": "x"}, {"a": [1], "c": 1}):
        with pytest.raises(SchemaError):
            validate(bad, s)


def test_schema_bool_is_not_a_number():
    with pytest.raises(SchemaError):
        validate(True, {"type": "number"})


# ----------------------------------------------------------- call loop
def test_successful_call_logs_one_attempt():
    a, calls = agent({"news_analyst|m|ACME": {"x": 3}})
    assert asyncio.run(a.call(payload={}, ticker="ACME")) == {"x": 3}
    assert calls[0].success and calls[0].attempts == 1 and calls[0].key == "ACME"


def test_parse_error_then_recovery_sends_feedback():
    a, calls = agent({"news_analyst|m|*": ["not json at all", {"x": 1}]})
    assert asyncio.run(a.call(payload={})) == {"x": 1}
    assert calls[0].attempts == 2
    last_msgs = a.provider.calls[-1]["messages"]
    assert "not valid JSON" in last_msgs[-1]["content"]
    assert last_msgs[-2] == {"role": "assistant", "content": "not json at all"}


def test_validation_error_then_recovery():
    a, _ = agent({"news_analyst|m|*": [{"x": -1}, {"x": 2}]})
    assert asyncio.run(a.call(payload={})) == {"x": 2}
    assert "failed validation" in a.provider.calls[-1]["messages"][-1]["content"]


def test_validation_error_exhausts_retries():
    a, calls = agent({"news_analyst|m|*": {"y": 1}})
    with pytest.raises(AgentValidationError):
        asyncio.run(a.call(payload={}))
    assert len(a.provider.calls) == 3 and not calls[0].success
    assert calls[0].error_type == "AgentValidationError"


def test_parse_error_exhausts_retries():
    a, _ = agent({"news_analyst|m|*": "nope"})
    with pytest.raises(AgentParseError):
        asyncio.run(a.call(payload={}))


def test_provider_error_becomes_call_error():
    a, _ = agent({"news_analyst|m|*": {"__error__": "HTTP 502"}})
    with pytest.raises(AgentCallError) as e:
        asyncio.run(a.call(payload={}))
    assert isinstance(e.value, AgentError) and "HTTP 502" in str(e.value)


def test_transient_provider_error_retried():
    a, _ = agent({"news_analyst|m|*": [{"__error__": "timeout"}, {"x": 5}]})
    assert asyncio.run(a.call(payload={})) == {"x": 5}


def test_error_hierarchy():
    for cls in (AgentParseError, AgentValidationError, AgentCallError):
        assert issubclass(cls, AgentError)


def test_prompt_loading_and_reload():
    a, _ = agent({})
    text = a.load_prompt()
    assert "NEWS CONTEXT ANALYST" in text and a.reload_prompt() == text


def test_missing_prompt_raises():
    a = JsonRoleAgent(MockProvider(), agent_name="does_not_exist", schema=SCHEMA)
    with pytest.raises(AgentError):
        a.load_prompt()


def test_messages_carry_payload_and_schema():
    a, _ = agent({})
    msgs = a.build_messages(payload={"ticker": "ACME"})
    assert msgs[0]["role"] == "system" and '"ticker": "ACME"' in msgs[1]["content"]
    assert json.dumps(SCHEMA, sort_keys=True) in msgs[1]["content"]


# ----------------------------------------------------------- providers
def test_mock_wildcard_resolution_order():
    m = MockProvider({"a|*|*": "any", "a|m|*": "model", "a|*|K": "key", "a|m|K": "exact"})
    run = lambda mo, k: asyncio.run(m.complete(mo, [], metadata={"agent": "a", "key": k})).text  # noqa: E731
    assert run("m", "K") == "exact"
    assert run("m", "Z") == "model"
    assert run("q", "K") == "key"
    assert run("q", "Z") == "any"


def test_mock_missing_response_is_provider_error():
    with pytest.raises(ProviderError):
        asyncio.run(MockProvider().complete("m", [], metadata={"agent": "x"}))


def test_get_provider_requires_key_without_mock(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        get_provider(mock=False)
    assert get_provider(mock=True).name == "mock"


def test_openrouter_reads_key_only_from_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    p = OpenRouterProvider(base_url="http://127.0.0.1:9")
    with pytest.raises(ProviderError, match="not set"):
        asyncio.run(p.complete("m", [{"role": "user", "content": "hi"}]))


def test_openrouter_parses_envelope(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    p = OpenRouterProvider(base_url="http://127.0.0.1:9")
    monkeypatch.setattr(p, "_post", lambda payload, timeout: {
        "choices": [{"message": {"content": ' {"x": 1} '}}], "usage": {"total_tokens": 7}})
    c = asyncio.run(p.complete("m", []))
    assert c.text == '{"x": 1}' and c.usage == {"total_tokens": 7}
    monkeypatch.setattr(p, "_post", lambda payload, timeout: {"error": "nope"})
    with pytest.raises(ProviderError):
        asyncio.run(p.complete("m", []))
