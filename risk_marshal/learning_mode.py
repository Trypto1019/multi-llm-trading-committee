"""Learning-mode enforcer and profit-milestone tracker.

A middle ground between full Risk Marshal enforcement and no enforcement, used
during an aggressive paper-trading learning phase:

- TRIM_AUTO at 25% of NAV (emergency; runs before any model is asked)
- TRIM_RECOMMEND at 15% of NAV (the arbiter may confirm)
- BLOCK new trades that would push a position above 10% of NAV
- BLOCK new entries into a sector already at or above 25% of NAV
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from risk_marshal.constants import LearningModeConstants as LMC


class LearningModeStatus(Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    TRIM_RECOMMEND = "TRIM_RECOMMEND"
    TRIM_AUTO = "TRIM_AUTO"
    WARN = "WARN"


@dataclass(frozen=True)
class LearningModeVerdict:
    status: LearningModeStatus
    ticker: str
    reason: str
    position_pct_nav: float
    sector: str | None = None
    sector_pct_nav: float = 0.0
    trim_recommendation: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ticker": self.ticker,
            "reason": self.reason,
            "position_pct_nav": round(self.position_pct_nav, 2),
            "sector": self.sector,
            "sector_pct_nav": round(self.sector_pct_nav, 2),
            "trim_recommendation": self.trim_recommendation,
            "warnings": list(self.warnings),
            "timestamp": self.timestamp.isoformat(),
        }


ENTRY_ACTIONS = ("BUY", "SHORT", "ADD")


class LearningModeEnforcer:
    """Concentration guard with an injectable ticker -> sector map."""

    def __init__(self, sector_map: Mapping[str, str] | None = None) -> None:
        self._sector_map: dict[str, str] = {k.upper(): v for k, v in (sector_map or {}).items()}
        self._sector_exposure: dict[str, float] = {}

    def get_sector(self, ticker: str) -> str:
        return self._sector_map.get(ticker.upper(), "Other")

    def calculate_sector_exposures(self, positions: list[Mapping[str, Any]], nav: float) -> dict[str, float]:
        """Sector exposure as % of NAV. Positions carry `ticker` and `value` (or `market_value`)."""
        values: dict[str, float] = {}
        for pos in positions:
            ticker = str(pos.get("ticker", "")).upper()
            value = abs(float(pos.get("value", 0) or pos.get("market_value", 0) or 0))
            sector = self.get_sector(ticker)
            values[sector] = values.get(sector, 0.0) + value
        self._sector_exposure = {s: (v / nav * 100) if nav > 0 else 0.0 for s, v in values.items()}
        return dict(self._sector_exposure)

    @staticmethod
    def _trim(position_value: float, nav: float, target_pct: float, current_pct: float, reason: str) -> dict:
        target_value = nav * (target_pct / 100)
        return {
            "target_pct": target_pct,
            "target_value": round(target_value, 2),
            "trim_value": round(position_value - target_value, 2),
            "current_value": round(position_value, 2),
            "current_pct": round(current_pct, 2),
            "reason": reason,
        }

    def check_existing_position(
        self, ticker: str, position_value: float, nav: float, is_risk_off: bool = False
    ) -> LearningModeVerdict:
        ticker = ticker.upper()
        pct = (position_value / nav * 100) if nav > 0 else 0.0
        sector = self.get_sector(ticker)
        sector_pct = self._sector_exposure.get(sector, 0.0)

        if pct >= LMC.EMERGENCY_POSITION_CEILING_PCT:
            return LearningModeVerdict(
                LearningModeStatus.TRIM_AUTO, ticker,
                f"EMERGENCY: Position {pct:.1f}% exceeds {LMC.EMERGENCY_POSITION_CEILING_PCT:.0f}% ceiling",
                pct, sector, sector_pct,
                self._trim(position_value, nav, LMC.TRIM_TO_PCT, pct, "TRIM_AUTO_EMERGENCY"))

        if pct >= LMC.EXISTING_POSITION_CEILING_PCT:
            target = LMC.TRIM_TO_PCT_RISK_OFF if is_risk_off else LMC.TRIM_TO_PCT
            return LearningModeVerdict(
                LearningModeStatus.TRIM_RECOMMEND, ticker,
                f"Position {pct:.1f}% exceeds {LMC.EXISTING_POSITION_CEILING_PCT:.0f}% ceiling",
                pct, sector, sector_pct,
                self._trim(position_value, nav, target, pct,
                           "TRIM_RECOMMEND" + ("_RISK_OFF" if is_risk_off else "")))

        warnings: tuple[str, ...] = ()
        if pct >= LMC.WARN_POSITION_PCT:
            warnings = (f"Position {pct:.1f}% exceeds soft limit ({LMC.WARN_POSITION_PCT:.0f}%)",)
        return LearningModeVerdict(
            LearningModeStatus.WARN if warnings else LearningModeStatus.PASS, ticker,
            warnings[0] if warnings else "OK", pct, sector, sector_pct, warnings=warnings)

    def check_new_trade(
        self, ticker: str, action: str, trade_value: float, current_position_value: float, nav: float
    ) -> LearningModeVerdict:
        ticker, action = ticker.upper(), action.upper()
        sector = self.get_sector(ticker)
        sector_pct = self._sector_exposure.get(sector, 0.0)

        if action not in ENTRY_ACTIONS:
            return LearningModeVerdict(
                LearningModeStatus.PASS, ticker, f"Exit action '{action}' allowed", 0.0, sector, sector_pct)

        new_pct = ((current_position_value + trade_value) / nav * 100) if nav > 0 else 0.0
        if new_pct > LMC.NEW_TRADE_CEILING_PCT:
            return LearningModeVerdict(
                LearningModeStatus.BLOCK, ticker,
                f"BLOCKED: Would push position to {new_pct:.1f}% (ceiling {LMC.NEW_TRADE_CEILING_PCT:.0f}%)",
                new_pct, sector, sector_pct)
        if sector_pct >= LMC.SECTOR_CEILING_PCT:
            return LearningModeVerdict(
                LearningModeStatus.BLOCK, ticker,
                f"BLOCKED: Sector '{sector}' at {sector_pct:.1f}% (ceiling {LMC.SECTOR_CEILING_PCT:.0f}%)",
                new_pct, sector, sector_pct)

        warnings: tuple[str, ...] = ()
        if new_pct >= LMC.WARN_POSITION_PCT:
            warnings = (f"Trade would push position to {new_pct:.1f}%",)
        return LearningModeVerdict(
            LearningModeStatus.WARN if warnings else LearningModeStatus.PASS, ticker,
            warnings[0] if warnings else "OK", new_pct, sector, sector_pct, warnings=warnings)

    def scan_all_positions(
        self, positions: list[Mapping[str, Any]], nav: float, is_risk_off: bool = False
    ) -> dict[str, Any]:
        """Scan every position; emergency trims come back first so they can run before any model."""
        self.calculate_sector_exposures(positions, nav)
        emergency, recommend, soft = [], [], []
        for pos in positions:
            value = abs(float(pos.get("value", 0) or pos.get("market_value", 0) or 0))
            v = self.check_existing_position(str(pos.get("ticker", "")), value, nav, is_risk_off)
            if v.status is LearningModeStatus.TRIM_AUTO:
                emergency.append(v)
            elif v.status is LearningModeStatus.TRIM_RECOMMEND:
                recommend.append(v)
            elif v.status is LearningModeStatus.WARN:
                soft.append(v)
        sector_warnings = [
            {"sector": s, "pct_nav": round(p, 1), "status": "BLOCKED_FOR_NEW_ENTRIES"}
            for s, p in self._sector_exposure.items() if p >= LMC.SECTOR_CEILING_PCT
        ]
        return {
            "emergency_trims": emergency,
            "trim_recommendations": recommend,
            "soft_warnings": soft,
            "sector_warnings": sector_warnings,
            "sector_exposure": dict(self._sector_exposure),
            "summary": {
                "emergency_count": len(emergency),
                "recommend_count": len(recommend),
                "soft_warning_count": len(soft),
                "sector_warning_count": len(sector_warnings),
            },
        }


class ProfitMilestoneTracker:
    """Records when positions cross 5/10/15/20/25% profit and how much was given back."""

    MILESTONES = LMC.PROFIT_MILESTONES

    @staticmethod
    def calculate_profit_pct(entry_price: float, current_price: float, direction: str) -> float:
        if entry_price <= 0:
            return 0.0
        if direction.upper() == "SHORT":
            return ((entry_price - current_price) / entry_price) * 100
        return ((current_price - entry_price) / entry_price) * 100

    @classmethod
    def get_milestones_to_record(cls, profit_pct: float, existing: list[int]) -> list[int]:
        return [m for m in cls.MILESTONES if profit_pct >= m and m not in existing]

    @staticmethod
    def calculate_giveback(peak_profit_pct: float, current_profit_pct: float) -> float:
        if peak_profit_pct <= 0:
            return 0.0
        return max(0.0, peak_profit_pct - current_profit_pct)
