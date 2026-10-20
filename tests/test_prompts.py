"""Lint the role prompts: boundaries present, contracts parseable, nothing business-specific."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from committee.roles import ROLES

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
WELL_KNOWN_TICKERS = {"AAPL", "NVDA", "MSFT", "SPY", "QQQ", "TSLA", "AMZN", "META", "GOOGL", "BA", "XLF", "XLE"}


@pytest.mark.parametrize("role", sorted(ROLES))
def test_every_role_has_a_prompt(role):
    assert (PROMPTS / f"{role}.md").exists()


@pytest.mark.parametrize("path", sorted(PROMPTS.glob("*.md")), ids=lambda p: p.stem)
class TestPrompt:
    def test_has_negative_boundaries(self, path):
        assert "You do NOT" in path.read_text(encoding="utf-8")

    def test_has_strict_json_contract_that_parses(self, path):
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, re.S)
        assert blocks, "prompt must show its JSON output contract"
        for b in blocks:
            json.loads(b)

    def test_no_money_amounts_or_real_tickers(self, path):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\$\s?\d", text), "no account sizes or dollar figures in prompts"
        words = set(re.findall(r"\b[A-Z]{2,5}\b", text))
        assert not (words & WELL_KNOWN_TICKERS)


def test_arbiter_prompt_lists_exactly_the_code_whitelist():
    from consensus.arbiter import VETO_WHITELIST
    text = (PROMPTS / "arbiter.md").read_text(encoding="utf-8")
    section = text.split("## VETO WHITELIST")[1].split("##")[0]
    listed = set(re.findall(r"^- ([A-Z_0-9]+)", section, re.M))
    assert listed == set(VETO_WHITELIST)
