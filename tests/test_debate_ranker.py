from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from debate import Candidate, DebateLedger, DebateRanker, LeakGuardError, assign_sides, brier, report, spearman
from providers import MockProvider

NOW = datetime(2026, 3, 6, 20, 0, tzinfo=timezone.utc)
ADV = ("model/a", "model/b")


def cand(cid="C1", price=0.5, opens=NOW + timedelta(days=1)):
    return Candidate(cid, "Does XYZ close above 10?", "XYZ above 10", "XYZ at or below 10", price, opens)


def provider(cid="C1"):
    return MockProvider({
        f"debate_research|*|{cid}": "neutral brief",
        f"debate_advocate|*|{cid}:backed": "case for",
        f"debate_advocate|*|{cid}:other": "case against",
        f"debate_blind_judge|*|{cid}": {"p_backed": 0.62, "key_factor": "k"},
        f"debate_priced_judge|*|{cid}": {"score": 8, "rationale": "r"},
    })


def test_side_assignment_is_stable_and_complete():
    a = assign_sides("C1", ADV)
    assert a == assign_sides("C1", ADV) and set(a) == set(ADV)


def test_side_assignment_is_roughly_balanced():
    firsts = [assign_sides(f"case-{i}", ADV)[0] for i in range(400)]
    share = firsts.count(ADV[0]) / len(firsts)
    assert 0.4 < share < 0.6


def test_full_pipeline_record():
    p = provider()
    rec = asyncio.run(DebateRanker(p).score(cand(price=0.55), as_of=NOW))
    assert rec["blind_p_backed"] == 0.62 and rec["score"] == 8 and rec["blind_edge"] == pytest.approx(0.07)
    assert rec["outcome"] is None and "PAPER" in rec["note"]
    assert {rec["advocate_backed"], rec["advocate_other"]} == set(DebateRanker(p).config.advocates)
    order = [c["agent"] for c in p.calls]
    assert order[0] == "debate_research" and order[-2:] == ["debate_blind_judge", "debate_priced_judge"]


def test_blind_judge_never_sees_the_price():
    p = provider()
    asyncio.run(DebateRanker(p).score(cand(price=0.437), as_of=NOW))
    blind = next(c for c in p.calls if c["agent"] == "debate_blind_judge")
    priced = next(c for c in p.calls if c["agent"] == "debate_priced_judge")
    blind_text = blind["messages"][0]["content"]
    assert "0.44" not in blind_text and "THE MARKET" not in blind_text
    assert "0.44" in priced["messages"][0]["content"] and "0.62" in priced["messages"][0]["content"]


def test_leak_guard_refuses_started_events():
    with pytest.raises(LeakGuardError):
        asyncio.run(DebateRanker(provider()).score(cand(opens=NOW), as_of=NOW))


def test_score_many_logs_skips():
    p = provider("C1")
    scored, skipped = asyncio.run(DebateRanker(p).score_many(
        [cand("C1"), cand("C2", opens=NOW - timedelta(hours=1)), cand("C3")], as_of=NOW))
    assert [r["case_id"] for r in scored] == ["C1"]
    assert {s["case_id"] for s in skipped} == {"C2", "C3"}
    assert any(s["reason"].startswith("leak_guard") for s in skipped)
    assert any(s["reason"].startswith("call_error") for s in skipped)


@pytest.mark.parametrize("price", [0, 1, -0.1, 1.5])
def test_candidate_price_must_be_probability(price):
    with pytest.raises(ValueError):
        cand(price=price)


def test_candidate_requires_aware_datetime():
    with pytest.raises(ValueError):
        Candidate("C", "q", "a", "b", 0.5, datetime(2026, 1, 1))


def test_ledger_scores_once(tmp_path):
    led = DebateLedger(tmp_path / "l.jsonl")
    assert led.append({"case_id": "C1", "score": 5})
    assert not led.append({"case_id": "C1", "score": 9})
    assert led.load() == [{"case_id": "C1", "score": 5}]


def test_spearman_and_brier():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert spearman([1, 2], [1, 2]) is None
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([], []) is None


def test_report_pools_only_graded():
    recs = [
        {"score": 8, "outcome": 1, "blind_p_backed": 0.7, "price": 0.5, "advocate_backed": "a"},
        {"score": 2, "outcome": 0, "blind_p_backed": 0.3, "price": 0.5, "advocate_backed": "b"},
        {"score": 6, "outcome": 1, "blind_p_backed": 0.6, "price": 0.5, "advocate_backed": "a"},
        {"score": 5, "outcome": None, "blind_p_backed": 0.5, "price": 0.5, "advocate_backed": "b"},
    ]
    r = report(recs)
    assert r["n_scored"] == 4 and r["n_graded"] == 3
    assert r["brier_blind"] < r["brier_market"]
    assert r["mean_blind_p_by_backed_advocate"] == {"a": 0.65, "b": 0.3}
    assert report([]) == {"n_scored": 0, "n_graded": 0}
