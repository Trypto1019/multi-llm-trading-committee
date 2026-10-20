"""Deterministic translation from a council consensus into a trade card.

No model touches this step. A proposal exists only when:
- there is a strict-majority direction (not MIXED),
- the plurality action reaches the same quorum, and
- that action is consistent with the direction (no "BULLISH + SHORT").
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from typing import Any

from consensus.council import CouncilResult

# Normalise the council's vocabulary to the executor's four order sides.
ACTION_TO_SIDE = {"BUY": "BUY", "ADD": "BUY", "SHORT": "SHORT", "SELL": "SELL",
                  "CUT": "SELL", "TRIM": "SELL", "COVER": "COVER"}
SIDES_FOR_DIRECTION = {"BULLISH": {"BUY", "COVER"}, "BEARISH": {"SHORT", "SELL"}}

# Confidence tiers -> requested size (% of NAV). The Risk Marshal still caps it.
SIZE_TIERS: tuple[tuple[float, float], ...] = ((0.70, 3.0), (0.55, 2.0), (0.0, 1.0))


@dataclass(frozen=True)
class Proposal:
    card_id: str
    ticker: str
    direction: str
    action: str
    size_percent: float
    entry: float
    stop_loss: float | None
    take_profit: float | None
    mean_confidence: float
    agreement: str
    sector: str | None = None

    def to_trade_card(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "ticker": self.ticker,
            "action": self.action,
            "size_percent": self.size_percent,
            "entry": self.entry,
            "stop_loss": self.stop_loss or 0,
            "take_profit": self.take_profit,
            "sector": self.sector,
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class NoTrade:
    ticker: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "no_trade": True, "reason": self.reason}


def size_for_confidence(confidence: float) -> float:
    for floor, size in SIZE_TIERS:
        if confidence >= floor:
            return size
    return SIZE_TIERS[-1][1]


def build_proposal(result: CouncilResult, last_price: float, sector: str | None = None) -> Proposal | NoTrade:
    c = result.consensus
    if not c.has_majority:
        return NoTrade(result.ticker, f"no {c.quorum}/{c.n_members} majority on direction")
    side = ACTION_TO_SIDE.get(c.action_consensus)
    if side is None:
        return NoTrade(result.ticker, f"consensus action {c.action_consensus} is not an order")
    if c.action_agreement < c.quorum:
        return NoTrade(result.ticker, f"action agreement {c.agreement} below quorum")
    if side not in SIDES_FOR_DIRECTION[c.direction]:
        return NoTrade(result.ticker, f"action {c.action_consensus} contradicts {c.direction} majority")

    agreeing = [o for o in result.opinions
                if o.ok and o.direction == c.direction and ACTION_TO_SIDE.get(o.action or "") == side]
    confidence = statistics.fmean(o.confidence or 0.0 for o in agreeing)
    stops = [o.stop_loss for o in agreeing if o.stop_loss]
    targets = [o.take_profit for o in agreeing if o.take_profit]
    return Proposal(
        card_id=uuid.uuid4().hex[:12],
        ticker=result.ticker,
        direction=c.direction,
        action=side,
        size_percent=size_for_confidence(confidence),
        entry=round(last_price, 2),
        stop_loss=round(statistics.median(stops), 2) if stops else None,
        take_profit=round(statistics.median(targets), 2) if targets else None,
        mean_confidence=round(confidence, 3),
        agreement=c.agreement,
        sector=sector,
    )
