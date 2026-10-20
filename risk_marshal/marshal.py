"""The Risk Marshal.

PURE PYTHON. NO AI. NO LLM. NO APPEALS. NO EXCEPTIONS.

This module enforces hard trading limits that cannot be overridden, persuaded,
or bypassed by any other component. It sits *after* every language model in
the pipeline: models propose trade cards, this code disposes of them.

The Risk Marshal has one job: protect the portfolio from ruin.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from risk_marshal.constants import RiskConstants
from risk_marshal.models import (
    DrawdownMode,
    PortfolioHealthReport,
    PortfolioState,
    Position,
    RiskStatus,
    RiskVerdict,
    Severity,
    Violation,
    ViolationType,
)


def _num(value: Any) -> float:
    """Coerce a trade-card number; anything unusable becomes 0 (and then fails validation)."""
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class RiskMarshal:
    """Every trade must pass through here before an order intent is written.

    No configuration. No customization. Rules are rules.
    """

    # ------------------------------------------------------------------ main
    def validate_trade(
        self,
        trade_card: Mapping[str, Any],
        portfolio: PortfolioState,
        universe: Iterable[str],
        vix: float | None = None,
    ) -> RiskVerdict:
        """Validate a proposed trade card against every risk rule.

        trade_card keys: ticker, action (BUY|SELL|SHORT|COVER), size_percent
        (% of NAV), entry, stop_loss, optional card_id and sector.
        """
        violations: list[Violation] = []
        warnings: list[str] = []

        ticker = str(trade_card.get("ticker") or "").strip().upper()
        action = str(trade_card.get("action") or "").strip().upper()
        size_pct = _num(trade_card.get("size_percent"))
        entry = _num(trade_card.get("entry"))
        stop_loss = _num(trade_card.get("stop_loss"))
        card_id = trade_card.get("card_id")
        universe_list = [str(t).upper() for t in universe]

        original_size_pct = size_pct
        adjusted_size_pct = size_pct

        def verdict(status: RiskStatus, approved: float | None) -> RiskVerdict:
            changed = adjusted_size_pct != original_size_pct
            return RiskVerdict(
                status=status,
                violations=tuple(violations),
                warnings=tuple(warnings),
                adjusted_size_pct=adjusted_size_pct if changed else None,
                original_size_pct=original_size_pct if changed else None,
                approved_size_pct=approved,
                trade_card_id=card_id,
                ticker=ticker,
                current_nav=portfolio.nav,
                current_drawdown_pct=portfolio.drawdown_pct,
                drawdown_mode=portfolio.drawdown_mode,
                vix_level=vix,
            )

        # STEP 0: trade card structure ---------------------------------------
        if not ticker:
            violations.append(Violation(
                ViolationType.INVALID_TRADE_CARD, "Trade card missing ticker", Severity.CRITICAL))
        if action not in RiskConstants.VALID_ACTIONS:
            violations.append(Violation(
                ViolationType.INVALID_ACTION, f"Invalid action: {action or '<empty>'}", Severity.CRITICAL))
        if size_pct <= 0:
            violations.append(Violation(
                ViolationType.INVALID_TRADE_CARD, "Trade card has invalid size_percent", Severity.CRITICAL))
        if entry <= 0:
            violations.append(Violation(
                ViolationType.INVALID_TRADE_CARD, "Trade card has invalid entry price", Severity.CRITICAL))
        if portfolio.nav <= 0:
            violations.append(Violation(
                ViolationType.INVALID_TRADE_CARD, "Portfolio NAV must be positive", Severity.CRITICAL))
        if violations:
            return verdict(RiskStatus.FAIL, None)

        # STEP 1: drawdown mode ------------------------------------------------
        dd_mode = portfolio.drawdown_mode
        dd_pct = portfolio.drawdown_pct
        if dd_mode is DrawdownMode.DEFENSIVE:
            if action in RiskConstants.OPENING_ACTIONS:
                violations.append(Violation(
                    ViolationType.DRAWDOWN_DEFENSIVE,
                    f"Portfolio is in DEFENSIVE mode ({dd_pct:.1f}% drawdown). Only SELL/COVER allowed.",
                    Severity.CRITICAL, {"drawdown_pct": dd_pct}))
        elif dd_mode is DrawdownMode.FREEZE:
            if action == "BUY":
                violations.append(Violation(
                    ViolationType.DRAWDOWN_FREEZE,
                    f"Portfolio is in FREEZE mode ({dd_pct:.1f}% drawdown). No new buys allowed.",
                    Severity.CRITICAL, {"drawdown_pct": dd_pct}))
        elif dd_mode is DrawdownMode.WARNING:
            warnings.append(f"Portfolio drawdown at {dd_pct:.1f}% - approaching freeze threshold")

        # STEP 2: VIX / market regime -----------------------------------------
        if vix is not None:
            if vix >= RiskConstants.VIX_FREEZE_THRESHOLD:
                if action in RiskConstants.OPENING_ACTIONS:
                    violations.append(Violation(
                        ViolationType.VIX_FREEZE,
                        f"VIX at {vix:.1f} (>= {RiskConstants.VIX_FREEZE_THRESHOLD:.0f}). No new positions allowed.",
                        Severity.CRITICAL, {"vix": vix}))
            elif vix >= RiskConstants.VIX_CAUTION_THRESHOLD:
                adjusted_size_pct = size_pct * RiskConstants.VIX_SIZE_REDUCTION
                warnings.append(
                    f"VIX at {vix:.1f} (>= {RiskConstants.VIX_CAUTION_THRESHOLD:.0f}). Position size reduced "
                    f"from {size_pct:.1f}% to {adjusted_size_pct:.1f}%")

        # STEP 3: universe (buys only) ----------------------------------------
        if action == "BUY" and ticker not in universe_list:
            violations.append(Violation(
                ViolationType.TICKER_NOT_IN_UNIVERSE,
                f"{ticker} is not in today's approved universe",
                Severity.HIGH, {"universe_size": len(universe_list)}))

        # STEP 4: position size -----------------------------------------------
        if adjusted_size_pct > RiskConstants.MAX_POSITION_PCT:
            violations.append(Violation(
                ViolationType.POSITION_SIZE_EXCEEDED,
                f"Position size {adjusted_size_pct:.1f}% exceeds max {RiskConstants.MAX_POSITION_PCT}%",
                Severity.HIGH,
                {"requested_pct": adjusted_size_pct, "max_pct": RiskConstants.MAX_POSITION_PCT}))

        position_value = (adjusted_size_pct / 100) * portfolio.nav
        if position_value < RiskConstants.MIN_POSITION_VALUE:
            violations.append(Violation(
                ViolationType.POSITION_TOO_SMALL,
                f"Position value {position_value:.0f} below minimum {RiskConstants.MIN_POSITION_VALUE:.0f}",
                Severity.MEDIUM,
                {"position_value": position_value, "minimum": RiskConstants.MIN_POSITION_VALUE}))

        # STEP 5: stop loss (long entries) ------------------------------------
        if stop_loss > 0 and action == "BUY":
            stop_distance_pct = ((entry - stop_loss) / entry) * 100
            if stop_distance_pct > RiskConstants.MAX_STOP_LOSS_PCT:
                violations.append(Violation(
                    ViolationType.STOP_LOSS_TOO_WIDE,
                    f"Stop loss {stop_distance_pct:.1f}% from entry exceeds max {RiskConstants.MAX_STOP_LOSS_PCT}%",
                    Severity.HIGH,
                    {"entry": entry, "stop_loss": stop_loss, "distance_pct": stop_distance_pct}))
            elif stop_distance_pct < 0:
                violations.append(Violation(
                    ViolationType.INVALID_TRADE_CARD,
                    "Stop loss is above entry price for a BUY order",
                    Severity.CRITICAL, {"entry": entry, "stop_loss": stop_loss}))

        # STEP 6: sector exposure ---------------------------------------------
        ticker_sector = self._get_ticker_sector(ticker, portfolio.positions) or trade_card.get("sector")
        if ticker_sector and action == "BUY":
            current_sector_value = portfolio.sectors.get(ticker_sector, 0.0)
            new_sector_pct = ((current_sector_value + position_value) / portfolio.nav) * 100
            if new_sector_pct > RiskConstants.MAX_SECTOR_PCT:
                violations.append(Violation(
                    ViolationType.SECTOR_EXPOSURE_EXCEEDED,
                    f"Sector {ticker_sector} exposure would be {new_sector_pct:.1f}% "
                    f"(max {RiskConstants.MAX_SECTOR_PCT}%)",
                    Severity.HIGH,
                    {"sector": ticker_sector,
                     "current_pct": (current_sector_value / portfolio.nav) * 100,
                     "new_pct": new_sector_pct}))

        # STEP 7: correlation -------------------------------------------------
        if action == "BUY":
            correlated = self._count_correlated_positions(
                ticker, portfolio.positions, portfolio.correlation_matrix)
            if correlated >= RiskConstants.MAX_CORRELATED_POSITIONS:
                violations.append(Violation(
                    ViolationType.CORRELATION_LIMIT_EXCEEDED,
                    f"Already have {correlated} positions correlated with {ticker} "
                    f"(max {RiskConstants.MAX_CORRELATED_POSITIONS})",
                    Severity.HIGH, {"correlated_count": correlated}))

        # STEP 8: daily limits ------------------------------------------------
        if action == "BUY":
            if portfolio.today_new_positions >= RiskConstants.MAX_NEW_POSITIONS_PER_DAY:
                violations.append(Violation(
                    ViolationType.DAILY_POSITION_LIMIT,
                    f"Already opened {portfolio.today_new_positions} new positions today "
                    f"(max {RiskConstants.MAX_NEW_POSITIONS_PER_DAY})",
                    Severity.HIGH, {"today_count": portfolio.today_new_positions}))
            deployed_pct = ((portfolio.today_deployed + position_value) / portfolio.nav) * 100
            if deployed_pct > RiskConstants.MAX_DAILY_DEPLOYMENT_PCT:
                violations.append(Violation(
                    ViolationType.DAILY_DEPLOYMENT_LIMIT,
                    f"Daily deployment would be {deployed_pct:.1f}% (max {RiskConstants.MAX_DAILY_DEPLOYMENT_PCT}%)",
                    Severity.HIGH,
                    {"already_deployed": portfolio.today_deployed,
                     "this_trade": position_value, "total_pct": deployed_pct}))

        # FINAL VERDICT -------------------------------------------------------
        if violations:
            return verdict(RiskStatus.FAIL, None)
        if warnings:
            return verdict(RiskStatus.WARNING, adjusted_size_pct)
        return verdict(RiskStatus.PASS, adjusted_size_pct)

    # ------------------------------------------------------- health check
    def validate_portfolio_health(self, portfolio: PortfolioState) -> PortfolioHealthReport:
        """Pre-market portfolio health check. Run it before any model is consulted."""
        issues: list[str] = []
        dd_pct = portfolio.drawdown_pct
        dd_mode = portfolio.drawdown_mode

        if dd_mode is DrawdownMode.DEFENSIVE:
            issues.append(f"CRITICAL: Portfolio in DEFENSIVE mode ({dd_pct:.1f}% drawdown)")
        elif dd_mode is DrawdownMode.FREEZE:
            issues.append(f"WARNING: Portfolio in FREEZE mode ({dd_pct:.1f}% drawdown)")
        elif dd_mode is DrawdownMode.WARNING:
            issues.append(f"CAUTION: Drawdown at {dd_pct:.1f}%")

        largest_pos_pct, largest_pos_ticker = 0.0, None
        for pos in portfolio.positions:
            pct = (pos.market_value / portfolio.nav) * 100 if portfolio.nav > 0 else 0.0
            if pct > largest_pos_pct:
                largest_pos_pct, largest_pos_ticker = pct, pos.symbol
        if largest_pos_pct > RiskConstants.MAX_POSITION_PCT:
            issues.append(f"Position {largest_pos_ticker} at {largest_pos_pct:.1f}% exceeds limit")

        largest_sector_pct, largest_sector = 0.0, None
        for sector, value in portfolio.sectors.items():
            pct = (value / portfolio.nav) * 100 if portfolio.nav > 0 else 0.0
            if pct > largest_sector_pct:
                largest_sector_pct, largest_sector = pct, sector
        if largest_sector_pct > RiskConstants.MAX_SECTOR_PCT:
            issues.append(f"Sector {largest_sector} at {largest_sector_pct:.1f}% exceeds limit")

        groups = self._find_correlated_groups(portfolio.positions, portfolio.correlation_matrix)
        for group in groups:
            if len(group) > RiskConstants.MAX_CORRELATED_POSITIONS:
                issues.append(f"Correlated group too large: {', '.join(group)}")

        return PortfolioHealthReport(
            timestamp=datetime.now(timezone.utc),
            nav=portfolio.nav,
            high_water_mark=portfolio.high_water_mark,
            drawdown_pct=dd_pct,
            drawdown_mode=dd_mode,
            num_positions=len(portfolio.positions),
            largest_position_pct=largest_pos_pct,
            largest_position_ticker=largest_pos_ticker,
            largest_sector_pct=largest_sector_pct,
            largest_sector=largest_sector,
            highly_correlated_groups=groups,
            healthy=not issues,
            issues=issues,
        )

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _get_ticker_sector(ticker: str, positions: Iterable[Position]) -> str | None:
        for pos in positions:
            if pos.symbol.upper() == ticker.upper():
                return pos.sector
        return None

    @staticmethod
    def _lookup_corr(matrix: Mapping[tuple[str, str], float], a: str, b: str) -> float:
        for key in ((a, b), (b, a)):
            if key in matrix:
                return float(matrix[key])
        return 0.0

    def _count_correlated_positions(
        self,
        ticker: str,
        positions: Iterable[Position],
        correlation_matrix: Mapping[tuple[str, str], float],
    ) -> int:
        ticker = ticker.upper()
        count = 0
        for pos in positions:
            other = pos.symbol.upper()
            if other == ticker:
                continue
            if abs(self._lookup_corr(correlation_matrix, ticker, other)) >= RiskConstants.CORRELATION_THRESHOLD:
                count += 1
        return count

    def _find_correlated_groups(
        self,
        positions: Iterable[Position],
        correlation_matrix: Mapping[tuple[str, str], float],
    ) -> list[list[str]]:
        """Connected components of the |r| >= threshold graph (groups of size > 1)."""
        tickers = [p.symbol.upper() for p in positions if p.symbol]
        adjacency: dict[str, set[str]] = {t: set() for t in tickers}
        for i, t1 in enumerate(tickers):
            for t2 in tickers[i + 1:]:
                if abs(self._lookup_corr(correlation_matrix, t1, t2)) >= RiskConstants.CORRELATION_THRESHOLD:
                    adjacency[t1].add(t2)
                    adjacency[t2].add(t1)

        visited: set[str] = set()
        groups: list[list[str]] = []
        for start in tickers:
            if start in visited:
                continue
            group: list[str] = []
            queue = deque([start])
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                group.append(current)
                queue.extend(n for n in adjacency[current] if n not in visited)
            if len(group) > 1:
                groups.append(sorted(group))
        return groups

    # -------------------------------------------------------- quick checks
    def can_buy(self, portfolio: PortfolioState, vix: float | None = None) -> tuple[bool, str]:
        dd_mode = portfolio.drawdown_mode
        if dd_mode is DrawdownMode.DEFENSIVE:
            return False, f"Portfolio in DEFENSIVE mode ({portfolio.drawdown_pct:.1f}% drawdown)"
        if dd_mode is DrawdownMode.FREEZE:
            return False, f"Portfolio in FREEZE mode ({portfolio.drawdown_pct:.1f}% drawdown)"
        if vix is not None and vix >= RiskConstants.VIX_FREEZE_THRESHOLD:
            return False, f"VIX at {vix:.1f} exceeds freeze threshold"
        if portfolio.today_new_positions >= RiskConstants.MAX_NEW_POSITIONS_PER_DAY:
            return False, f"Daily position limit reached ({portfolio.today_new_positions})"
        return True, "OK"

    def max_position_size(self, portfolio: PortfolioState, vix: float | None = None) -> float:
        """Maximum allowed position value in account currency."""
        max_pct = RiskConstants.MAX_POSITION_PCT
        if vix is not None and vix >= RiskConstants.VIX_CAUTION_THRESHOLD:
            max_pct *= RiskConstants.VIX_SIZE_REDUCTION
        return (max_pct / 100) * portfolio.nav

    def remaining_daily_deployment(self, portfolio: PortfolioState) -> float:
        max_daily = (RiskConstants.MAX_DAILY_DEPLOYMENT_PCT / 100) * portfolio.nav
        return max(0.0, max_daily - portfolio.today_deployed)


_marshal_instance: RiskMarshal | None = None


def get_risk_marshal() -> RiskMarshal:
    """The Marshal is a singleton: there is only one source of truth."""
    global _marshal_instance
    if _marshal_instance is None:
        _marshal_instance = RiskMarshal()
    return _marshal_instance
