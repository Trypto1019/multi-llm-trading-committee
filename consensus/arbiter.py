"""Veto-only arbiter.

The arbiter is a model that reviews the council's proposal with the full
context. Its authority is deliberately narrow, and that narrowness is enforced
here in code, not just requested in its prompt:

- It may APPROVE or VETO. It can never flip direction or change the action;
  any attempt is stripped and recorded.
- A veto counts only if its reason code is on the whitelist of *risk* reasons.
  Second-guessing the majority ("technicals disagree", "regime unfavorable")
  is explicitly rejected: the veto is ignored and the rejection is recorded.
- Machine-readable data blockers (stale quotes, toxic spreads, no fill,
  earnings inside the window) veto deterministically, whatever the model says.
- If the arbiter call fails, the ruling fails closed (HOLD) by default.

Everything that survives still has to pass the Risk Marshal afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from consensus.proposal import Proposal

VETO_WHITELIST: frozenset[str] = frozenset({
    "POSITION_LIMIT",          # position would exceed the per-name cap
    "SECTOR_EXPOSURE",         # sector/correlation cap would be breached
    "R_MULTIPLE_BELOW_1",      # reward does not cover risk
    "EARNINGS_WITHIN_3_DAYS",  # binary event inside the holding window
    "STRUCTURE_CONFLICT",      # trader flagged a strong structure conflict
    "DATA_STALENESS",          # quotes older than the freshness limit
    "SPREAD_TOXIC",            # bid/ask spread too wide to trade
    "NO_FILL",                 # fill feasibility says it will not fill
    "DATA_QUALITY_POOR",       # inputs too incomplete to trust
})

# Named here so they are rejected loudly rather than falling through as "unknown".
FORBIDDEN_VETO_REASONS: frozenset[str] = frozenset({
    "TECHNICALS_DISAGREE", "REGIME_UNFAVORABLE", "MOMENTUM_CONTRADICTS", "GUT_FEEL",
})

# Blocker types that veto deterministically when present in the input data.
HARD_BLOCKER_TYPES: frozenset[str] = frozenset({
    "DATA_STALENESS", "SPREAD_TOXIC", "NO_FILL", "DATA_QUALITY_POOR", "EARNINGS_WITHIN_3_DAYS",
})

ARBITER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "direction", "final_action", "blockers", "summary"],
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "VETO"]},
        "veto_reason_code": {"type": ["string", "null"]},
        "veto_detail": {"type": ["string", "null"]},
        "direction": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL", "MIXED"]},
        "final_action": {"type": "string"},
        "blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "reason"],
                "properties": {"type": {"type": "string"}, "reason": {"type": "string"},
                               "unblock": {"type": ["string", "null"]}},
            },
        },
        "top_risks": {"type": "array", "items": {"type": "string"}},
        "what_changes_my_mind": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}


@dataclass(frozen=True)
class Blocker:
    type: str
    reason: str
    source: str  # "data" | "arbiter"
    unblock: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ArbiterRuling:
    approved: bool
    direction: str  # always the council's direction
    final_action: str  # the proposal's action, or HOLD
    veto_reason_code: str | None = None
    veto_detail: str | None = None
    blockers: tuple[Blocker, ...] = ()
    rejected: tuple[str, ...] = ()  # arbiter overreach that was stripped
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "direction": self.direction,
            "final_action": self.final_action,
            "veto_reason_code": self.veto_reason_code,
            "veto_detail": self.veto_detail,
            "blockers": [b.to_dict() for b in self.blockers],
            "rejected": list(self.rejected),
            "summary": self.summary,
        }


def data_blockers(market: Mapping[str, Any]) -> list[Blocker]:
    """Collect machine-readable blockers from the market snapshot for one ticker."""
    found: list[Blocker] = []
    for raw in market.get("blockers", []) or []:
        btype = str(raw.get("type", "")).upper()
        if btype in HARD_BLOCKER_TYPES:
            found.append(Blocker(btype, str(raw.get("reason", "")), "data", raw.get("unblock")))
    days = market.get("earnings_in_days")
    if isinstance(days, (int, float)) and 0 <= days <= 3:
        found.append(Blocker("EARNINGS_WITHIN_3_DAYS", f"earnings in {days} day(s)", "data"))
    return found


def rule(
    proposal: Proposal,
    arbiter_output: Mapping[str, Any] | None,
    blockers_from_data: Iterable[Blocker] = (),
    *,
    arbiter_error: str | None = None,
    fail_closed: bool = True,
) -> ArbiterRuling:
    """Combine deterministic blockers and the arbiter's (bounded) opinion into a ruling."""
    rejected: list[str] = []
    hard = list(blockers_from_data)

    def veto(code: str, detail: str | None, blockers: list[Blocker], summary: str | None = None) -> ArbiterRuling:
        return ArbiterRuling(False, proposal.direction, "HOLD", code, detail, tuple(blockers),
                             tuple(rejected), summary)

    if hard:
        return veto(hard[0].type, hard[0].reason, hard, "deterministic data blocker")

    if arbiter_output is None:
        if fail_closed:
            return veto("ARBITER_UNAVAILABLE", arbiter_error or "no arbiter output", [],
                        "arbiter failed; failing closed")
        rejected.append(f"arbiter unavailable ({arbiter_error}); proceeding to Risk Marshal")
        return ArbiterRuling(True, proposal.direction, proposal.action, rejected=tuple(rejected))

    out = arbiter_output
    summary = out.get("summary")

    # 1. Direction / action are not the arbiter's to change.
    claimed_dir = str(out.get("direction", proposal.direction)).upper()
    if claimed_dir != proposal.direction:
        rejected.append(f"direction flip {proposal.direction}->{claimed_dir} ignored")
    claimed_action = str(out.get("final_action", proposal.action)).upper()
    if claimed_action not in (proposal.action, "HOLD"):
        rejected.append(f"action change {proposal.action}->{claimed_action} ignored")

    # 2. Blockers the arbiter itself reported (only whitelisted types count).
    arb_blockers: list[Blocker] = []
    for raw in out.get("blockers", []) or []:
        btype = str(raw.get("type", "")).upper()
        if btype in HARD_BLOCKER_TYPES:
            arb_blockers.append(Blocker(btype, str(raw.get("reason", "")), "arbiter", raw.get("unblock")))
        else:
            rejected.append(f"blocker type {btype or '<empty>'} not machine-recognised; ignored")
    if arb_blockers:
        return veto(arb_blockers[0].type, arb_blockers[0].reason, arb_blockers, summary)

    # 3. Veto, if any, must cite a whitelisted risk reason.
    if out.get("decision") == "VETO":
        code = str(out.get("veto_reason_code") or "").upper()
        if code in VETO_WHITELIST:
            return veto(code, out.get("veto_detail"), [], summary)
        label = "forbidden" if code in FORBIDDEN_VETO_REASONS else "non-whitelisted"
        rejected.append(f"{label} veto reason {code or '<empty>'} ignored")

    return ArbiterRuling(True, proposal.direction, proposal.action, rejected=tuple(rejected), summary=summary)


@dataclass
class ArbiterContext:
    """Everything the arbiter model sees, assembled in one place for the audit trail."""

    proposal: dict[str, Any]
    council: dict[str, Any]
    news: dict[str, Any] | None
    macro: dict[str, Any] | None
    market: dict[str, Any]
    veto_whitelist: list[str] = field(default_factory=lambda: sorted(VETO_WHITELIST))

    def to_payload(self) -> dict[str, Any]:
        return dict(self.__dict__)
