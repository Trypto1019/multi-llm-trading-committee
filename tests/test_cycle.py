"""End-to-end: one committee cycle on synthetic data with the mock provider."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import demo
from committee import CommitteeConfig, CommitteeCycle, MarketSnapshot
from providers import MockProvider, get_provider
from rules import RuleEngine

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "fake_market"


@pytest.fixture
def cycle_run(tmp_path):
    cycle = CommitteeCycle(get_provider(mock=True), CommitteeConfig.load(mock=True), tmp_path,
                           rules=RuleEngine.from_file(ROOT / "rules" / "sample_patterns.json"))
    summary = asyncio.run(cycle.run(MarketSnapshot.load(DATA)))
    return cycle, summary, {o["ticker"]: o for o in summary["outcomes"]}


def test_every_gate_is_exercised(cycle_run):
    _, _, out = cycle_run
    assert {t: (o["stage"], o["result"]) for t, o in out.items()} == {
        "ACME": ("intent", "INTENT"),           # 3/3 -> approved -> VIX-halved -> intent
        "BOLT": ("risk_marshal", "RISK_FAIL"),  # 2/3 -> forbidden veto ignored -> stop too wide
        "CRUX": ("proposal", "NO_TRADE"),       # split vote + failed member -> MIXED
        "DYNA": ("arbiter", "VETOED"),          # deterministic data blocker
        "EMBR": ("arbiter", "VETOED"),          # whitelisted veto; flip attempt stripped
        "GLOW": ("rules", "NO_TRADE"),          # SKIP pattern: catalyst without unanimity
    }


def test_vix_scaling_applied_to_intent(cycle_run):
    _, _, out = cycle_run
    intent = out["ACME"]["intent"]
    assert intent["size_pct"] == 1.5 and intent["notional"] == 1500.0 and intent["mode"] == "PAPER_INTENT_ONLY"


def test_pre_checks_run_before_models(cycle_run):
    cycle, summary, _ = cycle_run
    pre = summary["pre_checks"]
    assert pre["emergency_trims"][0]["ticker"] == "HALO"
    assert {s["ticker"]: s["action"] for s in pre["trailing_stops"]} == {"FLUX": "UPDATE", "HALO": "NOT_ELIGIBLE"}
    sources = [i["source"] for i in cycle.intents.intents]
    assert sources == ["emergency_trim", "trailing_stop", "committee"]


def test_audit_snapshots_written_for_every_ticker(cycle_run):
    cycle, _, _ = cycle_run
    d = cycle.audit.cycle_dir(cycle.cycle_id)
    assert sorted(p.stem for p in d.glob("*.json")) == ["ACME", "BOLT", "CRUX", "DYNA", "EMBR", "GLOW", "_cycle"]
    bolt = json.loads((d / "BOLT.json").read_text())
    assert bolt["arbiter"]["approved"] and "TECHNICALS_DISAGREE" in bolt["arbiter"]["rejected"][0]
    assert bolt["risk_verdict"]["violations"][0]["code"] == "STOP_LOSS_TOO_WIDE"
    embr = json.loads((d / "EMBR.json").read_text())
    assert embr["arbiter"]["direction"] == "BULLISH" and any("flip" in r for r in embr["arbiter"]["rejected"])


def test_dyna_blocker_skips_arbiter_model_call(cycle_run):
    cycle, _, _ = cycle_run
    arb_keys = [c.key for c in cycle.calls if c.agent == "arbiter"]
    assert "DYNA" not in arb_keys and "ACME" in arb_keys


def test_retry_and_failure_are_recorded(cycle_run):
    cycle, _, _ = cycle_run
    acme_gpt = [c for c in cycle.calls if c.key == "ACME" and c.model == "openai/gpt-4o"]
    assert acme_gpt[0].attempts == 2 and acme_gpt[0].success
    crux_grok = [c for c in cycle.calls if c.key == "CRUX" and c.model == "x-ai/grok-3"
                 and c.agent == "council_member"]
    assert not crux_grok[0].success and crux_grok[0].error_type == "AgentCallError"


def test_daily_counters_update_after_intent(tmp_path):
    cycle = CommitteeCycle(get_provider(mock=True), CommitteeConfig.load(mock=True), tmp_path)
    snap = MarketSnapshot.load(DATA)
    snap.candidates = ["ACME"]
    asyncio.run(cycle.run(snap))
    assert len([i for i in cycle.intents.intents if i["source"] == "committee"]) == 1


def test_arbiter_outage_fails_closed(tmp_path):
    mock = MockProvider.from_file(DATA / "mock_responses.json")
    mock.responses["arbiter|*|ACME"] = {"__error__": "HTTP 503"}
    cycle = CommitteeCycle(mock, CommitteeConfig.load(mock=True), tmp_path)
    snap = MarketSnapshot.load(DATA)
    snap.candidates = ["ACME"]
    out = asyncio.run(cycle.run(snap))["outcomes"][0]
    assert out["result"] == "VETOED" and "ARBITER_UNAVAILABLE" in out["detail"]


def test_demo_entrypoint_runs_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert asyncio.run(demo.main(["--mock", "--debate", "--out", str(tmp_path)])) == 0
    text = capsys.readouterr().out
    assert "PAPER" in text and "leak_guard" in text and "no alpha claimed" in text
    assert (tmp_path / "order_intents.jsonl").exists() and (tmp_path / "debate_ledger.jsonl").exists()


def test_demo_without_key_or_mock_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert asyncio.run(demo.main([])) == 2
    assert "--mock" in capsys.readouterr().err


def test_no_broker_code_anywhere():
    banned = ("ib_insync", "placeOrder", "alpaca", "broker_api")
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(b in text for b in banned), path
