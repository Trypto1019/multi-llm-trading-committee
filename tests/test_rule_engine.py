from __future__ import annotations

import json

import pytest

from rules import RuleEngine, TriggerSyntaxError, calculate_tier, evaluate, evaluate_trigger, parse

CTX = {
    "agents_bullish": 3,
    "adversary": {"concern_score": 0.25},
    "members": {"a": {"direction": "BULLISH", "confidence": 0.72}},
    "macro": {"vix": 18.5, "regime": "RISK_ON"},
    "news": {"catalyst_flag": False, "note": "a OR b"},
}


@pytest.mark.parametrize("expr, expected", [
    ("agents_bullish >= 3 AND adversary.concern_score < 0.30", True),
    ("agents_bullish == 4 AND adversary.concern_score < 0.25", False),
    ("members.a.direction == 'BULLISH'", True),
    ('members.a.direction == "BULLISH"', True),
    ("macro.vix > 20", False),
    ("macro.vix > 20 OR agents_bullish >= 3", True),
    ("agents_bullish >= 3 AND macro.vix > 20 OR macro.regime == 'RISK_ON'", True),  # AND binds tighter
    ("agents_bullish >= 3 AND (macro.vix > 20 OR macro.regime == 'RISK_OFF')", False),
    ("NOT macro.vix > 20", True),
    ("NOT (agents_bullish >= 3)", False),
    ("news.catalyst_flag == false", True),
    ("news.catalyst_flag", False),
    ("news.note == 'a OR b'", True),  # operators inside strings are not operators
    ("agents_bullish != 2", True),
    ("macro.vix >= -1", True),
    ("agents_bullish >= 3 and macro.vix < 20", True),  # keywords are case-insensitive
])
def test_trigger_evaluation(expr, expected):
    assert evaluate_trigger(expr, CTX) is expected


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hi')",
    "agents_bullish.__class__ == 1",
    "open('x')",
    "agents_bullish >= ",
    "(agents_bullish >= 3",
    "agents_bullish >= 3)",
    "agents_bullish >= 3 AND",
    "1 + 1 == 2",
    "",
    "x" * 600,
])
def test_unsafe_or_malformed_expressions_never_match(expr):
    r = evaluate(expr, CTX)
    assert r.matched is False and r.error


def test_missing_key_fails_closed():
    r = evaluate("missing.key > 1", CTX)
    assert not r.matched and "not found" in r.error


def test_type_mismatch_fails_closed():
    r = evaluate("members.a.direction > 3", CTX)
    assert not r.matched and r.error


def test_bool_not_orderable():
    assert evaluate("news.catalyst_flag < 1", CTX).error


def test_deep_nesting_rejected():
    with pytest.raises(TriggerSyntaxError):
        parse("(" * 40 + "agents_bullish > 1" + ")" * 40)


def test_snapshot_contains_referenced_values():
    r = evaluate("agents_bullish >= 3 AND macro.vix < 30", CTX)
    assert r.context_snapshot == {"agents_bullish": 3, "macro.vix": 18.5}


# ---------------------------------------------------------------- engine
@pytest.fixture
def engine():
    return RuleEngine(patterns={
        "SKIP_B": {"trigger": "macro.vix < 30", "action": "SKIP", "tier": "B"},
        "SKIP_A": {"trigger": "agents_bullish == 3", "action": "SKIP", "tier": "A"},
        "TRADE_C": {"trigger": "agents_bullish >= 2", "action": "TRADE", "tier": "C",
                    "live_stats": {"wins": 6, "losses": 4}},
        "TRADE_A": {"trigger": "agents_bullish >= 3", "action": "TRADE", "tier": "A"},
        "BROKEN": {"trigger": "nope >", "action": "SKIP", "tier": "A"},
    })


def test_find_matching_patterns(engine):
    ids = {m.pattern_id for m in engine.find_matching_patterns(CTX)}
    assert ids == {"SKIP_B", "SKIP_A", "TRADE_C", "TRADE_A"}
    c = next(m for m in engine.find_matching_patterns(CTX) if m.pattern_id == "TRADE_C")
    assert c.live_winrate == 0.6 and c.sample_size == 10


def test_best_pattern_prefers_tier(engine):
    assert engine.get_best_pattern(CTX).pattern_id == "TRADE_A"


def test_should_skip_returns_highest_tier(engine):
    skip, p = engine.should_skip_trade(CTX)
    assert skip and p.pattern_id == "SKIP_A"


def test_no_skip_when_nothing_matches():
    assert RuleEngine().should_skip_trade(CTX) == (False, None)


@pytest.mark.parametrize("wins, losses, p, tier", [
    (20, 10, 0.01, "A"),
    (17, 13, 0.01, "B"),
    (18, 12, 0.08, "B"),
    (12, 8, 0.2, "C"),
    (40, 0, None, "C"),  # no significance test, no tier
    (0, 0, 0.01, "C"),
])
def test_calculate_tier(wins, losses, p, tier):
    assert calculate_tier({"wins": wins, "losses": losses}, p) == tier


def test_update_live_stats_and_save(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"patterns": {"P": {"trigger": "agents_bullish > 0", "p_value": 0.01}}}))
    eng = RuleEngine.from_file(path)
    for _ in range(30):
        eng.update_live_stats("P", won=True, return_pct=1.0)
    assert eng.patterns["P"]["tier"] == "A"
    assert eng.update_live_stats("NOPE", True, 1.0) is False
    eng.save()
    assert json.loads(path.read_text())["patterns"]["P"]["live_stats"]["wins"] == 30


def test_sample_patterns_file_parses():
    from pathlib import Path
    eng = RuleEngine.from_file(Path(__file__).resolve().parent.parent / "rules" / "sample_patterns.json")
    for pid, p in eng.patterns.items():
        parse(p["trigger"])  # every shipped trigger must be syntactically valid
