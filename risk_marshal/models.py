"""Typed inputs and outputs for the Risk Marshal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from risk_marshal.constants import RiskConstants


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RiskStatus(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"


class DrawdownMode(Enum):
    NORMAL = "NORMAL"  # business as usual
    WARNING = "WARNING"  # 5-10% drawdown, proceed with caution
    FREEZE = "FREEZE"  # 10-15% drawdown, no new buys
    DEFENSIVE = "DEFENSIVE"  # >=15% drawdown, sell/cover only


class Severity(Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ViolationType(Enum):
    # Position limits
    POSITION_SIZE_EXCEEDED = "Position size exceeds 5% of NAV"
    SECTOR_EXPOSURE_EXCEEDED = "Sector exposure exceeds 20% of NAV"
    CORRELATION_LIMIT_EXCEEDED = "Too many correlated positions (>=3 with |r|>=0.7)"
    # Drawdown
    DRAWDOWN_FREEZE = "Portfolio in drawdown freeze mode (>=10%)"
    DRAWDOWN_DEFENSIVE = "Portfolio in defensive mode (>=15%), sells only"
    # Daily limits
    DAILY_POSITION_LIMIT = "Daily new position limit reached (3)"
    DAILY_DEPLOYMENT_LIMIT = "Daily deployment limit reached (10% NAV)"
    # Trade validation
    STOP_LOSS_TOO_WIDE = "Stop loss exceeds 5% from entry"
    POSITION_TOO_SMALL = "Position size below minimum value"
    TICKER_NOT_IN_UNIVERSE = "Ticker not in today's approved universe"
    # Market regime
    VIX_FREEZE = "VIX >= 35, no new positions allowed"
    # Structure
    INVALID_TRADE_CARD = "Trade card missing or invalid required fields"
    INVALID_ACTION = "Invalid trade action"


@dataclass(frozen=True)
class Violation:
    """A single, machine-readable risk violation."""

    type: ViolationType
    message: str
    severity: Severity
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.type.name,
            "type": self.type.value,
            "message": self.message,
            "severity": self.severity.value,
            "details": self.details,
        }


@dataclass(frozen=True)
class RiskVerdict:
    """The final verdict from the Risk Marshal. There is no appeal process."""

    status: RiskStatus
    violations: tuple[Violation, ...] = ()
    warnings: tuple[str, ...] = ()
    adjusted_size_pct: float | None = None  # set only when VIX scaling applied
    original_size_pct: float | None = None
    approved_size_pct: float | None = None  # size the executor may use (None on FAIL)
    trade_card_id: str | int | None = None
    ticker: str | None = None
    timestamp: datetime = field(default_factory=_utcnow)
    current_nav: float | None = None
    current_drawdown_pct: float | None = None
    drawdown_mode: DrawdownMode = DrawdownMode.NORMAL
    vix_level: float | None = None

    @property
    def passed(self) -> bool:
        return self.status is RiskStatus.PASS

    @property
    def failed(self) -> bool:
        return self.status is RiskStatus.FAIL

    @property
    def executable(self) -> bool:
        """PASS and WARNING may proceed; FAIL may not."""
        return self.status is not RiskStatus.FAIL

    def violation_codes(self) -> list[str]:
        return [v.type.name for v in self.violations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": list(self.warnings),
            "adjusted_size_pct": self.adjusted_size_pct,
            "original_size_pct": self.original_size_pct,
            "approved_size_pct": self.approved_size_pct,
            "trade_card_id": self.trade_card_id,
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "current_nav": self.current_nav,
            "current_drawdown_pct": self.current_drawdown_pct,
            "drawdown_mode": self.drawdown_mode.value,
            "vix_level": self.vix_level,
        }


@dataclass(frozen=True)
class Position:
    symbol: str
    market_value: float
    sector: str | None = None


@dataclass
class PortfolioState:
    """Current portfolio state for risk calculations.

    `positions` accepts `Position` objects or plain dicts with
    `symbol` / `market_value` / `sector` keys.
    """

    nav: float
    high_water_mark: float
    cash: float
    positions: list[Position | dict[str, Any]] = field(default_factory=list)
    sectors: dict[str, float] = field(default_factory=dict)  # {sector: market value}
    today_new_positions: int = 0
    today_deployed: float = 0.0  # value deployed today
    correlation_matrix: dict[tuple[str, str], float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.positions = [
            p if isinstance(p, Position) else Position(
                symbol=str(p.get("symbol", "")).upper(),
                market_value=float(p.get("market_value", 0) or 0),
                sector=p.get("sector"),
            )
            for p in self.positions
        ]

    @property
    def drawdown_pct(self) -> float:
        if self.high_water_mark <= 0:
            return 0.0
        return ((self.high_water_mark - self.nav) / self.high_water_mark) * 100

    @property
    def drawdown_mode(self) -> DrawdownMode:
        dd = self.drawdown_pct
        if dd >= RiskConstants.DRAWDOWN_DEFENSIVE_PCT:
            return DrawdownMode.DEFENSIVE
        if dd >= RiskConstants.DRAWDOWN_FREEZE_PCT:
            return DrawdownMode.FREEZE
        if dd >= RiskConstants.DRAWDOWN_WARNING_PCT:
            return DrawdownMode.WARNING
        return DrawdownMode.NORMAL


@dataclass(frozen=True)
class PortfolioHealthReport:
    timestamp: datetime
    nav: float
    high_water_mark: float
    drawdown_pct: float
    drawdown_mode: DrawdownMode
    num_positions: int
    largest_position_pct: float
    largest_position_ticker: str | None
    largest_sector_pct: float
    largest_sector: str | None
    highly_correlated_groups: list[list[str]]
    healthy: bool
    issues: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "nav": self.nav,
            "high_water_mark": self.high_water_mark,
            "drawdown_pct": round(self.drawdown_pct, 2),
            "drawdown_mode": self.drawdown_mode.value,
            "num_positions": self.num_positions,
            "largest_position_pct": round(self.largest_position_pct, 2),
            "largest_position_ticker": self.largest_position_ticker,
            "largest_sector_pct": round(self.largest_sector_pct, 2),
            "largest_sector": self.largest_sector,
            "highly_correlated_groups": self.highly_correlated_groups,
            "healthy": self.healthy,
            "issues": self.issues,
        }
