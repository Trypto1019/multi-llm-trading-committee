"""Parallel multi-model council with a strict-majority vote on direction.

Each member is an independent model given the *same* prompt. They run
concurrently (`asyncio.gather(..., return_exceptions=True)`), so one slow or
failing provider can neither block nor bias the others: a failed member is an
abstention, not a vote.

Consensus rules (3 members):
- direction: BULLISH or BEARISH needs 2 of 3 votes, otherwise MIXED
- action:    plurality of the actions given, reported as "n/3 agreement"
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agents.base_agent import AgentError, BaseAgent

DIRECTIONS = ("BULLISH", "NEUTRAL", "BEARISH")
ACTIONS = ("BUY", "ADD", "HOLD", "TRIM", "SELL", "CUT", "SHORT", "COVER", "NO_ACTION")

OPINION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["direction", "action", "confidence", "reasoning"],
    "properties": {
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "minLength": 1},
        "stop_loss": {"type": ["number", "null"], "minimum": 0},
        "take_profit": {"type": ["number", "null"], "minimum": 0},
    },
}


@dataclass(frozen=True)
class MemberOpinion:
    member: str
    model: str
    direction: str | None = None
    action: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class Consensus:
    direction: str  # BULLISH | BEARISH | MIXED
    bullish_count: int
    bearish_count: int
    action_consensus: str
    action_agreement: int
    n_members: int
    all_actions: tuple[str | None, ...]
    all_directions: tuple[str | None, ...]
    abstentions: int = 0

    @property
    def quorum(self) -> int:
        return self.n_members // 2 + 1

    @property
    def agreement(self) -> str:
        return f"{self.action_agreement}/{self.n_members}"

    @property
    def has_majority(self) -> bool:
        return self.direction != "MIXED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "bullish_count": self.bullish_count,
            "bearish_count": self.bearish_count,
            "action_consensus": self.action_consensus,
            "action_agreement": self.agreement,
            "quorum": self.quorum,
            "abstentions": self.abstentions,
            "all_actions": list(self.all_actions),
            "all_directions": list(self.all_directions),
        }


def calculate_consensus(opinions: Sequence[MemberOpinion]) -> Consensus:
    """Strict-majority direction plus plurality action.

    Abstentions (errored members) still count toward the denominator, so a
    council that lost a member needs *both* survivors to agree.
    Ties on action resolve to the first action seen, in member order.
    """
    n = len(opinions)
    if n == 0:
        raise ValueError("council has no members")
    quorum = n // 2 + 1
    directions = tuple(o.direction for o in opinions)
    actions = tuple(o.action for o in opinions)

    bullish = sum(1 for d in directions if d == "BULLISH")
    bearish = sum(1 for d in directions if d == "BEARISH")
    if bullish >= quorum:
        direction = "BULLISH"
    elif bearish >= quorum:
        direction = "BEARISH"
    else:
        direction = "MIXED"

    counts: dict[str, int] = {}
    for a in actions:
        if a:
            counts[a] = counts.get(a, 0) + 1
    if counts:
        top = max(counts.values())
        action = next(a for a in counts if counts[a] == top)
    else:
        top, action = 0, "HOLD"

    return Consensus(
        direction=direction,
        bullish_count=bullish,
        bearish_count=bearish,
        action_consensus=action,
        action_agreement=top,
        n_members=n,
        all_actions=actions,
        all_directions=directions,
        abstentions=sum(1 for o in opinions if not o.ok),
    )


@dataclass(frozen=True)
class CouncilMember:
    name: str
    agent: BaseAgent


@dataclass(frozen=True)
class CouncilResult:
    ticker: str
    opinions: tuple[MemberOpinion, ...]
    consensus: Consensus
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "opinions": [o.to_dict() for o in self.opinions],
            "consensus": self.consensus.to_dict(),
        }


class Council:
    """Runs every member concurrently on the same payload."""

    def __init__(self, members: Sequence[CouncilMember]) -> None:
        if len(members) < 3 or len(members) % 2 == 0:
            raise ValueError("a council needs an odd number of members, at least 3")
        self.members = list(members)

    async def deliberate(self, ticker: str, payload: dict[str, Any]) -> CouncilResult:
        results = await asyncio.gather(
            *(m.agent.call(payload=payload, ticker=ticker) for m in self.members),
            return_exceptions=True,
        )
        opinions = tuple(self._to_opinion(m, r) for m, r in zip(self.members, results))
        return CouncilResult(ticker=ticker, opinions=opinions, consensus=calculate_consensus(opinions))

    @staticmethod
    def _to_opinion(member: CouncilMember, result: Any) -> MemberOpinion:
        model = member.agent.model_id
        if isinstance(result, AgentError):
            return MemberOpinion(member=member.name, model=model, error=f"{type(result).__name__}: {result}")
        if isinstance(result, BaseException):
            # Anything that is not a typed agent error is a bug: surface it, do not vote.
            raise result
        return MemberOpinion(
            member=member.name,
            model=model,
            direction=result["direction"],
            action=result["action"],
            confidence=float(result["confidence"]),
            reasoning=result["reasoning"],
            stop_loss=result.get("stop_loss"),
            take_profit=result.get("take_profit"),
        )
