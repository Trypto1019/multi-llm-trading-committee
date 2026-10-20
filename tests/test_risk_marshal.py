"""Exhaustive tests for the deterministic Risk Marshal."""

from __future__ import annotations

import pytest

from risk_marshal import (
    DrawdownMode,
    PortfolioState,
    Position,
    RiskConstants,
    RiskMarshal,
    RiskStatus,
    Severity,
    ViolationType,
    get_risk_marshal,
)

UNIVERSE = ["ACME", "BOLT", "CRUX", "DYNA", "EMBR"]


def portfolio(**overrides) -> PortfolioState:
    base = dict(nav=100_000.0, high_water_mark=100_000.0, cash=50_000.0, positions=[], sectors={},
                today_new_positions=0, today_deployed=0.0, correlation_matrix={})
    base.update(overrides)
    return PortfolioState(**base)


def card(**overrides) -> dict:
    base = dict(card_id="c1", ticker="ACME", action="BUY", size_percent=3.0, entry=100.0, stop_loss=96.0)
    base.update(overrides)
    return base


@pytest.fixture
def rm() -> RiskMarshal:
    return RiskMarshal()


def codes(verdict) -> set[ViolationType]:
    return {v.type for v in verdict.violations}


# ----------------------------------------------------------------- constants
class TestConstants:
    def test_values_match_spec(self):
        assert RiskConstants.MAX_POSITION_PCT == 5.0
        assert RiskConstants.MAX_SECTOR_PCT == 20.0
        assert RiskConstants.MAX_CORRELATED_POSITIONS == 3
        assert RiskConstants.CORRELATION_THRESHOLD == 0.70
        assert (RiskConstants.DRAWDOWN_WARNING_PCT, RiskConstants.DRAWDOWN_FREEZE_PCT,
                RiskConstants.DRAWDOWN_DEFENSIVE_PCT) == (5.0, 10.0, 15.0)
        assert RiskConstants.MAX_NEW_POSITIONS_PER_DAY == 3
        assert RiskConstants.MAX_DAILY_DEPLOYMENT_PCT == 10.0
        assert RiskConstants.MAX_STOP_LOSS_PCT == 5.0
        assert (RiskConstants.VIX_CAUTION_THRESHOLD, RiskConstants.VIX_FREEZE_THRESHOLD) == (25.0, 35.0)
        assert RiskConstants.VIX_SIZE_REDUCTION == 0.5

    def test_cannot_be_reassigned(self):
        with pytest.raises(AttributeError):
            RiskConstants.MAX_POSITION_PCT = 50.0
        assert RiskConstants.MAX_POSITION_PCT == 5.0

    def test_cannot_be_deleted(self):
        with pytest.raises(AttributeError):
            del RiskConstants.MAX_SECTOR_PCT

    def test_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            RiskConstants()

    def test_marshal_takes_no_configuration(self):
        with pytest.raises(TypeError):
            RiskMarshal(max_position_pct=10)  # type: ignore[call-arg]

    def test_singleton(self):
        assert get_risk_marshal() is get_risk_marshal()


# ----------------------------------------------------------------- happy path
def test_clean_trade_passes(rm):
    v = rm.validate_trade(card(), portfolio(), UNIVERSE, vix=18.0)
    assert v.status is RiskStatus.PASS and v.passed and not v.failed and v.executable
    assert v.violations == () and v.warnings == ()
    assert v.approved_size_pct == 3.0
    assert v.adjusted_size_pct is None and v.original_size_pct is None
    assert v.ticker == "ACME" and v.trade_card_id == "c1" and v.vix_level == 18.0


def test_ticker_and_action_are_normalised(rm):
    v = rm.validate_trade(card(ticker=" acme ", action="buy"), portfolio(), UNIVERSE)
    assert v.passed and v.ticker == "ACME"


def test_vix_optional(rm):
    assert rm.validate_trade(card(), portfolio(), UNIVERSE, vix=None).passed


# ------------------------------------------------------------ step 0 structure
@pytest.mark.parametrize("bad, expected", [
    ({"ticker": ""}, ViolationType.INVALID_TRADE_CARD),
    ({"ticker": None}, ViolationType.INVALID_TRADE_CARD),
    ({"action": "YOLO"}, ViolationType.INVALID_ACTION),
    ({"action": ""}, ViolationType.INVALID_ACTION),
    ({"size_percent": 0}, ViolationType.INVALID_TRADE_CARD),
    ({"size_percent": -2}, ViolationType.INVALID_TRADE_CARD),
    ({"size_percent": "lots"}, ViolationType.INVALID_TRADE_CARD),
    ({"size_percent": True}, ViolationType.INVALID_TRADE_CARD),
    ({"entry": 0}, ViolationType.INVALID_TRADE_CARD),
    ({"entry": None}, ViolationType.INVALID_TRADE_CARD),
])
def test_structural_failures_short_circuit(rm, bad, expected):
    v = rm.validate_trade(card(**bad), portfolio(), UNIVERSE, vix=40.0)
    assert v.failed and expected in codes(v)
    assert all(x.severity is Severity.CRITICAL for x in v.violations)
    # short-circuit: no later-stage checks (e.g. VIX freeze) are evaluated
    assert ViolationType.VIX_FREEZE not in codes(v)
    assert v.approved_size_pct is None


def test_missing_fields_collect_multiple_violations(rm):
    v = rm.validate_trade({}, portfolio(), UNIVERSE)
    assert len(v.violations) == 4


def test_non_positive_nav_fails_closed(rm):
    v = rm.validate_trade(card(), portfolio(nav=0.0), UNIVERSE)
    assert v.failed


@pytest.mark.parametrize("action", ["BUY", "SELL", "SHORT", "COVER"])
def test_all_valid_actions_accepted(rm, action):
    v = rm.validate_trade(card(action=action, stop_loss=0), portfolio(), UNIVERSE)
    assert ViolationType.INVALID_ACTION not in codes(v)


# ---------------------------------------------------------------- step 1 drawdown
@pytest.mark.parametrize("nav, mode", [
    (100_000, DrawdownMode.NORMAL),
    (95_001, DrawdownMode.NORMAL),
    (95_000, DrawdownMode.WARNING),
    (90_001, DrawdownMode.WARNING),
    (90_000, DrawdownMode.FREEZE),
    (85_001, DrawdownMode.FREEZE),
    (85_000, DrawdownMode.DEFENSIVE),
    (50_000, DrawdownMode.DEFENSIVE),
])
def test_drawdown_mode_boundaries(nav, mode):
    assert portfolio(nav=float(nav), high_water_mark=100_000.0).drawdown_mode is mode


def test_drawdown_with_zero_hwm_is_zero():
    assert portfolio(high_water_mark=0.0).drawdown_pct == 0.0


def test_warning_mode_passes_with_warning(rm):
    v = rm.validate_trade(card(), portfolio(nav=94_000.0), UNIVERSE)
    assert v.status is RiskStatus.WARNING and v.executable
    assert any("drawdown" in w for w in v.warnings)
    assert v.drawdown_mode is DrawdownMode.WARNING


def test_freeze_blocks_buy(rm):
    v = rm.validate_trade(card(), portfolio(nav=89_000.0), UNIVERSE)
    assert v.failed and ViolationType.DRAWDOWN_FREEZE in codes(v)


@pytest.mark.parametrize("action", ["SHORT", "SELL", "COVER"])
def test_freeze_allows_non_buy(rm, action):
    v = rm.validate_trade(card(action=action, stop_loss=0), portfolio(nav=89_000.0), UNIVERSE)
    assert ViolationType.DRAWDOWN_FREEZE not in codes(v)


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
def test_defensive_blocks_opening(rm, action):
    v = rm.validate_trade(card(action=action, stop_loss=0), portfolio(nav=84_000.0), UNIVERSE)
    assert v.failed and ViolationType.DRAWDOWN_DEFENSIVE in codes(v)


@pytest.mark.parametrize("action", ["SELL", "COVER"])
def test_defensive_allows_closing(rm, action):
    v = rm.validate_trade(card(action=action, stop_loss=0), portfolio(nav=84_000.0), UNIVERSE)
    assert ViolationType.DRAWDOWN_DEFENSIVE not in codes(v)
    assert v.executable


# ---------------------------------------------------------------- step 2 VIX
def test_vix_below_caution_no_change(rm):
    v = rm.validate_trade(card(size_percent=4.0), portfolio(), UNIVERSE, vix=24.99)
    assert v.passed and v.approved_size_pct == 4.0


def test_vix_caution_halves_size(rm):
    v = rm.validate_trade(card(size_percent=4.0), portfolio(), UNIVERSE, vix=25.0)
    assert v.status is RiskStatus.WARNING
    assert v.original_size_pct == 4.0 and v.adjusted_size_pct == 2.0 and v.approved_size_pct == 2.0


def test_vix_halving_can_rescue_oversized_trade(rm):
    # 8% requested -> 4% after VIX scaling -> under the 5% cap
    v = rm.validate_trade(card(size_percent=8.0), portfolio(), UNIVERSE, vix=30.0)
    assert ViolationType.POSITION_SIZE_EXCEEDED not in codes(v)
    assert v.approved_size_pct == 4.0


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
def test_vix_freeze_blocks_opening(rm, action):
    v = rm.validate_trade(card(action=action, stop_loss=0), portfolio(), UNIVERSE, vix=35.0)
    assert v.failed and ViolationType.VIX_FREEZE in codes(v)


@pytest.mark.parametrize("action", ["SELL", "COVER"])
def test_vix_freeze_allows_closing(rm, action):
    v = rm.validate_trade(card(action=action, stop_loss=0), portfolio(), UNIVERSE, vix=50.0)
    assert ViolationType.VIX_FREEZE not in codes(v)


def test_vix_freeze_does_not_scale(rm):
    v = rm.validate_trade(card(action="SELL", size_percent=4.0, stop_loss=0), portfolio(), UNIVERSE, vix=40.0)
    assert v.adjusted_size_pct is None


# ---------------------------------------------------------------- step 3 universe
def test_buy_outside_universe_fails(rm):
    v = rm.validate_trade(card(ticker="ZZZZ"), portfolio(), UNIVERSE)
    assert ViolationType.TICKER_NOT_IN_UNIVERSE in codes(v)
    assert next(x for x in v.violations if x.type is ViolationType.TICKER_NOT_IN_UNIVERSE).details["universe_size"] == 5


def test_universe_match_is_case_insensitive(rm):
    assert rm.validate_trade(card(), portfolio(), ["acme"]).passed


@pytest.mark.parametrize("action", ["SELL", "SHORT", "COVER"])
def test_universe_only_gates_buys(rm, action):
    v = rm.validate_trade(card(ticker="ZZZZ", action=action, stop_loss=0), portfolio(), UNIVERSE)
    assert ViolationType.TICKER_NOT_IN_UNIVERSE not in codes(v)


# ---------------------------------------------------------------- step 4 size
def test_size_exactly_at_cap_passes(rm):
    assert rm.validate_trade(card(size_percent=5.0), portfolio(), UNIVERSE).passed


def test_size_over_cap_fails(rm):
    v = rm.validate_trade(card(size_percent=5.01), portfolio(), UNIVERSE)
    assert ViolationType.POSITION_SIZE_EXCEEDED in codes(v)


def test_size_cap_applies_to_shorts_too(rm):
    v = rm.validate_trade(card(action="SHORT", size_percent=6.0, stop_loss=0), portfolio(), UNIVERSE)
    assert ViolationType.POSITION_SIZE_EXCEEDED in codes(v)


def test_position_below_minimum_value(rm):
    # 0.4% of 100k = 400 < 500
    v = rm.validate_trade(card(size_percent=0.4), portfolio(), UNIVERSE)
    assert ViolationType.POSITION_TOO_SMALL in codes(v)
    assert next(x for x in v.violations if x.type is ViolationType.POSITION_TOO_SMALL).severity is Severity.MEDIUM


def test_minimum_value_checked_after_vix_scaling(rm):
    # 0.8% -> 0.4% under VIX caution -> 400 < 500
    v = rm.validate_trade(card(size_percent=0.8), portfolio(), UNIVERSE, vix=26.0)
    assert ViolationType.POSITION_TOO_SMALL in codes(v)


# ---------------------------------------------------------------- step 5 stop
def test_stop_at_max_distance_passes(rm):
    assert rm.validate_trade(card(entry=100.0, stop_loss=95.0), portfolio(), UNIVERSE).passed


def test_stop_too_wide_fails(rm):
    v = rm.validate_trade(card(entry=100.0, stop_loss=94.9), portfolio(), UNIVERSE)
    assert ViolationType.STOP_LOSS_TOO_WIDE in codes(v)
    d = next(x for x in v.violations if x.type is ViolationType.STOP_LOSS_TOO_WIDE).details
    assert d["distance_pct"] == pytest.approx(5.1)


def test_stop_above_entry_for_buy_is_invalid(rm):
    v = rm.validate_trade(card(entry=100.0, stop_loss=101.0), portfolio(), UNIVERSE)
    assert ViolationType.INVALID_TRADE_CARD in codes(v)


def test_missing_stop_is_not_a_violation(rm):
    assert rm.validate_trade(card(stop_loss=0), portfolio(), UNIVERSE).passed


def test_stop_check_only_for_buys(rm):
    v = rm.validate_trade(card(action="SHORT", entry=100.0, stop_loss=120.0), portfolio(), UNIVERSE)
    assert ViolationType.STOP_LOSS_TOO_WIDE not in codes(v)


# ---------------------------------------------------------------- step 6 sector
def test_sector_cap_from_existing_position(rm):
    pf = portfolio(positions=[Position("ACME", 3_000.0, "Industrials")], sectors={"Industrials": 18_000.0})
    v = rm.validate_trade(card(size_percent=3.0), pf, UNIVERSE)  # 18k + 3k = 21% > 20%
    assert ViolationType.SECTOR_EXPOSURE_EXCEEDED in codes(v)


def test_sector_cap_from_trade_card_sector(rm):
    pf = portfolio(sectors={"Technology": 19_000.0})
    v = rm.validate_trade(card(ticker="BOLT", sector="Technology", size_percent=2.0), pf, UNIVERSE)
    assert ViolationType.SECTOR_EXPOSURE_EXCEEDED in codes(v)


def test_sector_exactly_at_cap_passes(rm):
    pf = portfolio(sectors={"Technology": 17_000.0})
    v = rm.validate_trade(card(ticker="BOLT", sector="Technology", size_percent=3.0), pf, UNIVERSE)
    assert ViolationType.SECTOR_EXPOSURE_EXCEEDED not in codes(v)


def test_unknown_sector_skips_check(rm):
    pf = portfolio(sectors={"Technology": 50_000.0})
    assert ViolationType.SECTOR_EXPOSURE_EXCEEDED not in codes(rm.validate_trade(card(), pf, UNIVERSE))


def test_sector_check_only_for_buys(rm):
    pf = portfolio(sectors={"Technology": 50_000.0})
    v = rm.validate_trade(card(action="SHORT", sector="Technology", stop_loss=0), pf, UNIVERSE)
    assert ViolationType.SECTOR_EXPOSURE_EXCEEDED not in codes(v)


def test_positions_accept_plain_dicts():
    pf = portfolio(positions=[{"symbol": "acme", "market_value": 1000, "sector": "Industrials"}])
    assert pf.positions[0] == Position("ACME", 1000.0, "Industrials")


# ---------------------------------------------------------------- step 7 correlation
def _corr_portfolio(n_correlated: int, r: float = 0.8) -> PortfolioState:
    names = ["P1", "P2", "P3", "P4"][:n_correlated]
    return portfolio(positions=[Position(n, 1_000.0) for n in names],
                     correlation_matrix={("ACME", n): r for n in names})


@pytest.mark.parametrize("n, blocked", [(0, False), (1, False), (2, False), (3, True), (4, True)])
def test_correlation_cap(rm, n, blocked):
    v = rm.validate_trade(card(), _corr_portfolio(n), UNIVERSE)
    assert (ViolationType.CORRELATION_LIMIT_EXCEEDED in codes(v)) is blocked


def test_negative_correlation_counts(rm):
    v = rm.validate_trade(card(), _corr_portfolio(3, r=-0.75), UNIVERSE)
    assert ViolationType.CORRELATION_LIMIT_EXCEEDED in codes(v)


def test_correlation_below_threshold_ignored(rm):
    v = rm.validate_trade(card(), _corr_portfolio(4, r=0.69), UNIVERSE)
    assert ViolationType.CORRELATION_LIMIT_EXCEEDED not in codes(v)


def test_correlation_lookup_either_key_order(rm):
    pf = portfolio(positions=[Position(n, 1.0) for n in ("P1", "P2", "P3")],
                   correlation_matrix={(n, "ACME"): 0.9 for n in ("P1", "P2", "P3")})
    assert rm._count_correlated_positions("ACME", pf.positions, pf.correlation_matrix) == 3


def test_existing_same_ticker_not_counted(rm):
    pf = portfolio(positions=[Position("ACME", 1.0)], correlation_matrix={("ACME", "ACME"): 1.0})
    assert rm._count_correlated_positions("ACME", pf.positions, pf.correlation_matrix) == 0


# ---------------------------------------------------------------- step 8 daily
def test_daily_position_count_limit(rm):
    v = rm.validate_trade(card(), portfolio(today_new_positions=3), UNIVERSE)
    assert ViolationType.DAILY_POSITION_LIMIT in codes(v)


def test_daily_position_count_under_limit(rm):
    assert rm.validate_trade(card(), portfolio(today_new_positions=2), UNIVERSE).passed


def test_daily_deployment_limit(rm):
    v = rm.validate_trade(card(size_percent=3.0), portfolio(today_deployed=7_500.0), UNIVERSE)
    assert ViolationType.DAILY_DEPLOYMENT_LIMIT in codes(v)


def test_daily_deployment_exactly_at_limit(rm):
    assert rm.validate_trade(card(size_percent=3.0), portfolio(today_deployed=7_000.0), UNIVERSE).passed


def test_daily_limits_only_for_buys(rm):
    v = rm.validate_trade(card(action="SELL", stop_loss=0),
                          portfolio(today_new_positions=10, today_deployed=99_000.0), UNIVERSE)
    assert v.passed


# ---------------------------------------------------------------- aggregation
def test_violations_accumulate_after_structure(rm):
    pf = portfolio(nav=89_000.0, high_water_mark=100_000.0, today_new_positions=3)
    v = rm.validate_trade(card(ticker="ZZZZ", size_percent=9.0, stop_loss=80.0), pf, UNIVERSE)
    assert {ViolationType.DRAWDOWN_FREEZE, ViolationType.TICKER_NOT_IN_UNIVERSE,
            ViolationType.POSITION_SIZE_EXCEEDED, ViolationType.STOP_LOSS_TOO_WIDE,
            ViolationType.DAILY_POSITION_LIMIT} <= codes(v)


def test_fail_beats_warning(rm):
    v = rm.validate_trade(card(size_percent=12.0), portfolio(), UNIVERSE, vix=26.0)  # 6% after halving
    assert v.failed and v.warnings and v.approved_size_pct is None


def test_verdict_is_immutable(rm):
    v = rm.validate_trade(card(), portfolio(), UNIVERSE)
    with pytest.raises(AttributeError):
        v.status = RiskStatus.PASS  # type: ignore[misc]


def test_verdict_serialises(rm):
    d = rm.validate_trade(card(entry=100, stop_loss=90), portfolio(), UNIVERSE, vix=20).to_dict()
    assert d["status"] == "FAIL" and d["violations"][0]["code"] == "STOP_LOSS_TOO_WIDE"
    assert d["drawdown_mode"] == "NORMAL" and isinstance(d["timestamp"], str)


def test_validation_is_deterministic(rm):
    a = rm.validate_trade(card(), portfolio(), UNIVERSE, vix=27).to_dict()
    b = rm.validate_trade(card(), portfolio(), UNIVERSE, vix=27).to_dict()
    a.pop("timestamp"), b.pop("timestamp")
    assert a == b


def test_inputs_not_mutated(rm):
    c, pf = card(), portfolio(today_deployed=1_000.0)
    before = (dict(c), pf.today_deployed, pf.today_new_positions)
    rm.validate_trade(c, pf, UNIVERSE)
    assert (c, pf.today_deployed, pf.today_new_positions) == before


# ---------------------------------------------------------------- health
def test_health_clean(rm):
    h = rm.validate_portfolio_health(portfolio(positions=[Position("ACME", 3_000.0, "Industrials")],
                                               sectors={"Industrials": 3_000.0}))
    assert h.healthy and h.issues == [] and h.largest_position_ticker == "ACME"


def test_health_flags_everything(rm):
    pf = portfolio(
        nav=84_000.0, high_water_mark=100_000.0,
        positions=[Position(n, 9_000.0, "Tech") for n in ("A1", "A2", "A3", "A4")],
        sectors={"Tech": 36_000.0},
        correlation_matrix={("A1", "A2"): 0.8, ("A2", "A3"): 0.8, ("A3", "A4"): 0.9},
    )
    h = rm.validate_portfolio_health(pf)
    assert not h.healthy and h.drawdown_mode is DrawdownMode.DEFENSIVE
    assert h.highly_correlated_groups == [["A1", "A2", "A3", "A4"]]
    text = " ".join(h.issues)
    assert "DEFENSIVE" in text and "exceeds limit" in text and "Correlated group too large" in text
    assert h.to_dict()["largest_sector"] == "Tech"


def test_correlated_groups_are_connected_components(rm):
    pos = [Position(n, 1.0) for n in ("A", "B", "C", "D", "E")]
    groups = rm._find_correlated_groups(pos, {("A", "B"): 0.9, ("D", "C"): -0.8})
    assert groups == [["A", "B"], ["C", "D"]]


# ---------------------------------------------------------------- quick checks
@pytest.mark.parametrize("pf_kwargs, vix, allowed", [
    ({}, None, True),
    ({"nav": 84_000.0}, None, False),
    ({"nav": 89_000.0}, None, False),
    ({}, 36.0, False),
    ({"today_new_positions": 3}, None, False),
])
def test_can_buy(rm, pf_kwargs, vix, allowed):
    ok, reason = rm.can_buy(portfolio(**pf_kwargs), vix)
    assert ok is allowed and reason


def test_max_position_size(rm):
    assert rm.max_position_size(portfolio()) == 5_000.0
    assert rm.max_position_size(portfolio(), vix=30.0) == 2_500.0


def test_remaining_daily_deployment(rm):
    assert rm.remaining_daily_deployment(portfolio(today_deployed=4_000.0)) == 6_000.0
    assert rm.remaining_daily_deployment(portfolio(today_deployed=40_000.0)) == 0.0
