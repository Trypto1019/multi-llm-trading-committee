"""Immutable risk constants.

DO NOT make these configurable via environment variables.
DO NOT add "override" parameters.
DO NOT add "emergency bypass" flags.

They exist to prevent catastrophic losses. Assignment at runtime raises.
"""

from __future__ import annotations

from typing import Any


class _Immutable(type):
    """Metaclass that turns class attributes into read-only constants."""

    def __setattr__(cls, name: str, value: Any) -> None:
        raise AttributeError(f"{cls.__name__}.{name} is immutable (rules are rules)")

    def __delattr__(cls, name: str) -> None:
        raise AttributeError(f"{cls.__name__}.{name} is immutable (rules are rules)")

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(f"{cls.__name__} is a namespace of constants; do not instantiate it")


class RiskConstants(metaclass=_Immutable):
    """Hard limits enforced by the Risk Marshal."""

    # Position limits
    MAX_POSITION_PCT: float = 5.0  # max 5% of NAV in any single position
    MAX_SECTOR_PCT: float = 20.0  # max 20% of NAV in any sector
    MAX_CORRELATED_POSITIONS: int = 3  # max 3 existing positions with |r| >= threshold
    CORRELATION_THRESHOLD: float = 0.70  # what counts as "correlated"

    # Drawdown controls (measured from the high-water mark)
    DRAWDOWN_WARNING_PCT: float = 5.0  # flag at 5% drawdown
    DRAWDOWN_FREEZE_PCT: float = 10.0  # block new buys at 10%
    DRAWDOWN_DEFENSIVE_PCT: float = 15.0  # sell/cover only at 15%

    # Daily limits
    MAX_NEW_POSITIONS_PER_DAY: int = 3
    MAX_DAILY_DEPLOYMENT_PCT: float = 10.0  # max 10% of NAV deployed in one day

    # Trade validation
    MAX_STOP_LOSS_PCT: float = 5.0  # stop cannot be more than 5% from entry
    MIN_POSITION_VALUE: float = 500.0  # minimum position value in account currency

    # Market regime (VIX)
    VIX_CAUTION_THRESHOLD: float = 25.0  # halve size above 25
    VIX_FREEZE_THRESHOLD: float = 35.0  # no new positions above 35
    VIX_SIZE_REDUCTION: float = 0.50

    VALID_ACTIONS: tuple[str, ...] = ("BUY", "SELL", "SHORT", "COVER")
    OPENING_ACTIONS: tuple[str, ...] = ("BUY", "SHORT")


class LearningModeConstants(metaclass=_Immutable):
    """Softer ceilings used during an aggressive paper-trading learning phase.

    Still prevents catastrophic concentration: the emergency trim runs before
    any model is consulted.
    """

    WARN_POSITION_PCT: float = 5.0  # log a warning only
    EXISTING_POSITION_CEILING_PCT: float = 15.0  # TRIM_RECOMMEND above this
    EMERGENCY_POSITION_CEILING_PCT: float = 25.0  # TRIM_AUTO (immediate, no model vote)
    NEW_TRADE_CEILING_PCT: float = 10.0  # BLOCK entries that would exceed this
    SECTOR_CEILING_PCT: float = 25.0  # BLOCK new entries into a sector above this
    TRIM_TO_PCT: float = 15.0  # target after an emergency/normal trim
    TRIM_TO_PCT_RISK_OFF: float = 10.0  # target when the macro regime is risk-off
    PROFIT_MILESTONES: tuple[int, ...] = (5, 10, 15, 20, 25)
