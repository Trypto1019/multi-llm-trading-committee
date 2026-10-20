"""Council majority, proposal building and the veto-only arbiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from agents import JsonRoleAgent
from consensus import (
    OPINION_SCHEMA,
    Blocker,
    Council,
    CouncilMember,
    CouncilResult,
    MemberOpinion,
    NoTrade,
    Proposal,
    build_proposal,
    calculate_consensus,
    data_blockers,
    rule,
    size_for_confidence,
)
from providers import MockProvider
from providers.base import Completion


def op(d, a, c=0.6, sl=None, tp=None, member="m"):
    return MemberOpinion(member=member, model="x", direction=d, action=a, confidence=c, reasoning="r",
                         stop_loss=sl, take_profit=tp)


def err(member="m"):
    return MemberOpinion(member=member, model="x", error="AgentCallError: down")


# --------------------------------------------------------------- consensus
@pytest.mark.parametrize("dirs, expected", [
    (("BULLISH", "BULLISH", "BULLISH"), "BULLISH"),
    (("BULLISH", "BULLISH", "BEARISH"), "BULLISH"),
    (("BEARISH", "NEUTRAL", "BEARISH"), "BEARISH"),
    (("BULLISH", "BEARISH", "NEUTRAL"), "MIXED"),
    (("BULLISH", "NEUTRAL", "NEUTRAL"), "MIXED"),
    (("NEUTRAL", "NEUTRAL", "NEUTRAL"), "MIXED"),
])
def test_two_of_three_direction(dirs, expected):
    c = calculate_consensus([op(d, "HOLD") for d in dirs])
    assert c.direction == expected
    assert c.has_majority is (expected != "MIXED")


def test_abstention_counts_in_denominator():
    c = calculate_consensus([op("BULLISH", "BUY"), op("BEARISH", "SHORT"), err()])
    assert c.direction == "MIXED" and c.abstentions == 1
    c2 = calculate_consensus([op("BULLISH", "BUY"), op("BULLISH", "BUY"), err()])
    assert c2.direction == "BULLISH" and c2.agreement == "2/3"


def test_all_failed_defaults_to_hold():
    c = calculate_consensus([err(), err(), err()])
    assert c.direction == "MIXED" and c.action_consensus == "HOLD" and c.action_agreement == 0


def test_plurality_action_and_agreement_string():
    c = calculate_consensus([op("BULLISH", "BUY"), op("BULLISH", "ADD"), op("BULLISH", "BUY")])
    assert c.action_consensus == "BUY" and c.agreement == "2/3" and c.quorum == 2


def test_action_tie_resolves_to_first_seen():
    c = calculate_consensus([op("NEUTRAL", "HOLD"), op("BULLISH", "BUY"), op("BEARISH", "SHORT")])
    assert c.action_consensus == "HOLD" and c.action_agreement == 1


def test_empty_council_rejected():
    with pytest.raises(ValueError):
        calculate_consensus([])


def test_five_member_quorum_is_three():
    c = calculate_consensus([op("BULLISH", "BUY")] * 2 + [op("BEARISH", "SHORT")] * 3)
    assert c.quorum == 3 and c.direction == "BEARISH"


# --------------------------------------------------------------- council run
class SlowProvider:
    """Each call sleeps; parallel execution should take ~1x, not 3x."""

    name = "slow"

    async def complete(self, model, messages, **kw):
        await asyncio.sleep(0.2)
        if model == "broken":
            from providers.base import ProviderError
            raise ProviderError("down")
        return Completion('{"direction":"BULLISH","action":"BUY","confidence":0.7,"reasoning":"ok"}', model)


def _council(provider, models=("a", "b", "c")):
    return Council([CouncilMember(m, JsonRoleAgent(provider, agent_name="council_member", schema=OPINION_SCHEMA,
                                                   model_id=m, retry_delay=0)) for m in models])


def test_council_runs_members_in_parallel():
    start = time.monotonic()
    res = asyncio.run(_council(SlowProvider()).deliberate("ACME", {"ticker": "ACME"}))
    assert time.monotonic() - start < 0.5
    assert res.consensus.direction == "BULLISH" and res.consensus.agreement == "3/3"


def test_failed_member_is_abstention_not_crash():
    res = asyncio.run(_council(SlowProvider(), ("a", "b", "broken")).deliberate("ACME", {}))
    bad = [o for o in res.opinions if not o.ok]
    assert len(bad) == 1 and "AgentCallError" in bad[0].error
    assert res.consensus.direction == "BULLISH" and res.consensus.abstentions == 1


def test_council_requires_odd_size():
    with pytest.raises(ValueError):
        _council(SlowProvider(), ("a", "b"))


def test_council_with_mock_provider_uses_model_keys():
    mock = MockProvider({
        "council_member|a|T": {"direction": "BEARISH", "action": "SHORT", "confidence": 0.6, "reasoning": "x"},
        "council_member|b|T": {"direction": "BEARISH", "action": "SHORT", "confidence": 0.6, "reasoning": "x"},
        "council_member|c|T": {"direction": "BULLISH", "action": "BUY", "confidence": 0.6, "reasoning": "x"},
    })
    res = asyncio.run(_council(mock).deliberate("T", {}))
    assert res.consensus.direction == "BEARISH" and res.consensus.action_consensus == "SHORT"


# --------------------------------------------------------------- proposal
def result(*ops):
    return CouncilResult("ACME", tuple(ops), calculate_consensus(ops))


def test_proposal_from_unanimous_buy():
    p = build_proposal(result(op("BULLISH", "BUY", 0.7, 48, 55), op("BULLISH", "BUY", 0.8, 47, 56),
                              op("BULLISH", "BUY", 0.75, 49, 54)), 50.0, "Industrials")
    assert isinstance(p, Proposal)
    assert (p.action, p.size_percent, p.stop_loss, p.take_profit, p.agreement) == ("BUY", 3.0, 48.0, 55.0, "3/3")
    assert p.to_trade_card()["sector"] == "Industrials"


def test_proposal_uses_only_agreeing_members():
    p = build_proposal(result(op("BULLISH", "BUY", 0.6, 48), op("BULLISH", "BUY", 0.6, 47),
                              op("NEUTRAL", "HOLD", 0.1, 10)), 50.0)
    assert p.stop_loss == 47.5 and p.mean_confidence == 0.6 and p.size_percent == 2.0


@pytest.mark.parametrize("ops, reason", [
    ((op("BULLISH", "BUY"), op("BEARISH", "SHORT"), op("NEUTRAL", "HOLD")), "majority"),
    ((op("BULLISH", "HOLD"), op("BULLISH", "HOLD"), op("BULLISH", "BUY")), "not an order"),
    ((op("BULLISH", "BUY"), op("BULLISH", "ADD"), op("BULLISH", "COVER")), "agreement"),
    ((op("BULLISH", "SHORT"), op("BULLISH", "SHORT"), op("BULLISH", "BUY")), "contradicts"),
])
def test_no_trade_cases(ops, reason):
    p = build_proposal(result(*ops), 50.0)
    assert isinstance(p, NoTrade) and reason in p.reason


def test_add_normalises_to_buy_and_counts_toward_side():
    p = build_proposal(result(op("BULLISH", "BUY"), op("BULLISH", "BUY"), op("BULLISH", "ADD")), 50.0)
    assert isinstance(p, Proposal) and p.action == "BUY"


@pytest.mark.parametrize("conf, size", [(0.9, 3.0), (0.7, 3.0), (0.69, 2.0), (0.55, 2.0), (0.3, 1.0)])
def test_size_tiers(conf, size):
    assert size_for_confidence(conf) == size


# --------------------------------------------------------------- arbiter
def proposal(direction="BULLISH", action="BUY"):
    return Proposal("id", "ACME", direction, action, 3.0, 50.0, 48.0, 55.0, 0.7, "3/3")


def arb(decision="APPROVE", code=None, direction="BULLISH", action="BUY", blockers=()):
    return {"decision": decision, "veto_reason_code": code, "veto_detail": "d", "direction": direction,
            "final_action": action, "blockers": list(blockers), "summary": "s"}


def test_approve_passes_through():
    r = rule(proposal(), arb())
    assert r.approved and r.final_action == "BUY" and r.rejected == ()


@pytest.mark.parametrize("code", ["POSITION_LIMIT", "R_MULTIPLE_BELOW_1", "EARNINGS_WITHIN_3_DAYS",
                                  "SECTOR_EXPOSURE", "STRUCTURE_CONFLICT", "DATA_STALENESS"])
def test_whitelisted_veto_holds(code):
    r = rule(proposal(), arb("VETO", code, action="HOLD"))
    assert not r.approved and r.final_action == "HOLD" and r.veto_reason_code == code


@pytest.mark.parametrize("code", ["TECHNICALS_DISAGREE", "REGIME_UNFAVORABLE", "MOMENTUM_CONTRADICTS"])
def test_forbidden_veto_is_ignored_and_logged(code):
    r = rule(proposal(), arb("VETO", code, action="HOLD"))
    assert r.approved and r.final_action == "BUY"
    assert any("forbidden" in x and code in x for x in r.rejected)


def test_unknown_veto_is_ignored():
    r = rule(proposal(), arb("VETO", "I_JUST_DONT_LIKE_IT"))
    assert r.approved and any("non-whitelisted" in x for x in r.rejected)


def test_empty_veto_code_ignored():
    assert rule(proposal(), arb("VETO", None)).approved


def test_arbiter_cannot_flip_direction():
    r = rule(proposal(), arb("APPROVE", direction="BEARISH", action="SHORT"))
    assert r.approved and r.direction == "BULLISH" and r.final_action == "BUY"
    assert any("direction flip" in x for x in r.rejected)
    assert any("action change" in x for x in r.rejected)


def test_flip_attempt_with_valid_veto_still_holds():
    r = rule(proposal(), arb("VETO", "R_MULTIPLE_BELOW_1", direction="BEARISH", action="SHORT"))
    assert not r.approved and r.final_action == "HOLD" and r.direction == "BULLISH"
    assert any("direction flip" in x for x in r.rejected)


def test_arbiter_reported_blocker_vetoes():
    r = rule(proposal(), arb(blockers=[{"type": "NO_FILL", "reason": "no liquidity"}]))
    assert not r.approved and r.veto_reason_code == "NO_FILL" and r.blockers[0].source == "arbiter"


def test_arbiter_unknown_blocker_type_ignored():
    r = rule(proposal(), arb(blockers=[{"type": "VIBES", "reason": "meh"}]))
    assert r.approved and any("VIBES" in x for x in r.rejected)


def test_data_blockers_override_approval():
    hard = [Blocker("DATA_STALENESS", "quote 180s old", "data")]
    r = rule(proposal(), arb(), hard)
    assert not r.approved and r.veto_reason_code == "DATA_STALENESS"


def test_fail_closed_on_arbiter_error():
    r = rule(proposal(), None, arbiter_error="timeout")
    assert not r.approved and r.veto_reason_code == "ARBITER_UNAVAILABLE"


def test_fail_open_option_still_defers_to_marshal():
    r = rule(proposal(), None, arbiter_error="timeout", fail_closed=False)
    assert r.approved and r.rejected


def test_data_blockers_extraction():
    found = data_blockers({"blockers": [{"type": "spread_toxic", "reason": "wide"}, {"type": "OTHER"}],
                           "earnings_in_days": 2})
    assert [b.type for b in found] == ["SPREAD_TOXIC", "EARNINGS_WITHIN_3_DAYS"]
    assert data_blockers({"earnings_in_days": 4}) == []
