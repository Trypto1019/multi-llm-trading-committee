"""Giveback-based trailing stops for profitable positions.

Rules (profit measured from entry at the current price):
- profit >= 15%: give back at most 35% of the peak open profit
- profit >= 25%: give back at most 25%
- profit >= 35%: give back at most 20%

Safety:
- Monotonic: a stop is only ever tightened, never loosened.
- No insta-trigger: the stop stays at least 0.2% away from the current price.
- Breakeven clamp: once trailing, the stop locks in at least +0.5% vs entry.
- Idempotent: a re-run with the same prices is a no-op (min move 0.3% / 0.05).
- The best-price watermark is persisted through a pluggable store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Direction = Literal["LONG", "SHORT"]
StopAction = Literal["UPDATE", "KEEP", "PLACE_NEW", "NOT_ELIGIBLE"]

# (profit_pct threshold, max giveback fraction), checked highest first
GIVEBACK_THRESHOLDS: tuple[tuple[float, float], ...] = ((35.0, 0.20), (25.0, 0.25), (15.0, 0.35))
CURRENT_PRICE_BUFFER = 0.002
BREAKEVEN_LOCK = 0.005
MIN_MOVE_PCT = 0.003
MIN_MOVE_ABS = 0.05


class WatermarkStore(Protocol):
    def get(self, position_id: str) -> float | None: ...
    def set(self, position_id: str, price: float) -> None: ...


class InMemoryWatermarkStore:
    def __init__(self) -> None:
        self._data: dict[str, float] = {}

    def get(self, position_id: str) -> float | None:
        return self._data.get(position_id)

    def set(self, position_id: str, price: float) -> None:
        self._data[position_id] = price


@dataclass(frozen=True)
class TrailingCalc:
    eligible: bool
    profit_pct: float
    stop_price: float | None = None
    giveback_limit: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class StopDecision:
    action: StopAction
    current_stop: float | None
    new_stop: float | None
    stop_type: str
    profit_pct: float
    best_price: float
    giveback_limit: float | None
    reason: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _check_direction(direction: str) -> Direction:
    d = direction.upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
    return d  # type: ignore[return-value]


def get_giveback_limit(profit_pct: float) -> float | None:
    for threshold, giveback in GIVEBACK_THRESHOLDS:
        if profit_pct >= threshold:
            return giveback
    return None


def profit_pct(entry: float, price: float, direction: Direction) -> float:
    if entry <= 0:
        raise ValueError("entry must be positive")
    move = (entry - price) if direction == "SHORT" else (price - entry)
    return move / entry * 100


def calculate_trailing_stop(entry: float, best_price: float, current_price: float,
                            direction: str) -> TrailingCalc:
    d = _check_direction(direction)
    pnl = profit_pct(entry, current_price, d)
    limit = get_giveback_limit(pnl)
    if limit is None:
        return TrailingCalc(False, pnl, reason=f"profit {pnl:.1f}% < {GIVEBACK_THRESHOLDS[-1][0]:.0f}% threshold")

    peak_profit = (entry - best_price) if d == "SHORT" else (best_price - entry)
    if peak_profit <= 0:
        return TrailingCalc(False, pnl, giveback_limit=limit, reason="no profit to protect")

    allowed = peak_profit * limit
    if d == "SHORT":
        stop = best_price + allowed
        stop = min(stop, entry * (1 - BREAKEVEN_LOCK))
        stop = max(stop, current_price * (1 + CURRENT_PRICE_BUFFER))
    else:
        stop = best_price - allowed
        stop = max(stop, entry * (1 + BREAKEVEN_LOCK))
        stop = min(stop, current_price * (1 - CURRENT_PRICE_BUFFER))

    return TrailingCalc(True, pnl, round(stop, 2), limit,
                        f"{limit * 100:.0f}% giveback at {pnl:.1f}% profit")


def should_update_stop(current_stop: float, new_stop: float, direction: str) -> tuple[bool, str]:
    d = _check_direction(direction)
    tighter = new_stop < current_stop if d == "SHORT" else new_stop > current_stop
    if not tighter:
        return False, "new stop not tighter"
    move = abs(new_stop - current_stop)
    min_move = max(MIN_MOVE_ABS, current_stop * MIN_MOVE_PCT)
    if move < min_move:
        return False, f"move {move:.2f} < min {min_move:.2f}"
    return True, f"tightening by {move:.2f} ({move / current_stop * 100:.1f}%)"


class TrailingStopManager:
    def __init__(self, store: WatermarkStore | None = None) -> None:
        self.store = store or InMemoryWatermarkStore()

    def update_best_price(self, position_id: str, price: float, direction: str) -> float:
        d = _check_direction(direction)
        existing = self.store.get(position_id)
        if existing is None:
            best = price
        else:
            best = min(existing, price) if d == "SHORT" else max(existing, price)
        if best != existing:
            self.store.set(position_id, best)
        return best

    def process_position(
        self,
        position_id: str,
        entry: float,
        current_price: float,
        direction: str,
        current_stop: float | None = None,
        intraday_best: float | None = None,
    ) -> StopDecision:
        """Main entry point. `intraday_best` is the day's high (long) / low (short)."""
        d = _check_direction(direction)
        watermark_input = current_price
        if intraday_best is not None:
            watermark_input = min(intraday_best, current_price) if d == "SHORT" else max(intraday_best, current_price)
        best = self.update_best_price(position_id, watermark_input, d)
        calc = calculate_trailing_stop(entry, best, current_price, d)

        if not calc.eligible or calc.stop_price is None:
            return StopDecision("NOT_ELIGIBLE", current_stop, None, "STATIC", calc.profit_pct, best,
                                calc.giveback_limit, calc.reason)
        if current_stop is None:
            return StopDecision("PLACE_NEW", None, calc.stop_price, "TRAILING_GIVEBACK", calc.profit_pct,
                                best, calc.giveback_limit, f"no stop exists; recommend {calc.stop_price:.2f}")
        update, why = should_update_stop(current_stop, calc.stop_price, d)
        return StopDecision("UPDATE" if update else "KEEP", current_stop,
                            calc.stop_price if update else None, "TRAILING_GIVEBACK",
                            calc.profit_pct, best, calc.giveback_limit, why)
