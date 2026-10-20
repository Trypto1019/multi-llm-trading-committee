from __future__ import annotations

import pytest

from risk_marshal import (
    LearningModeConstants,
    LearningModeEnforcer,
    LearningModeStatus,
    ProfitMilestoneTracker,
)

SECTORS = {"ACME": "Industrials", "BOLT": "Technology", "FLUX": "Technology"}


@pytest.fixture
def lm() -> LearningModeEnforcer:
    return LearningModeEnforcer(SECTORS)


def test_constants_immutable():
    with pytest.raises(AttributeError):
        LearningModeConstants.EMERGENCY_POSITION_CEILING_PCT = 90.0


def test_unknown_ticker_is_other(lm):
    assert lm.get_sector("zzzz") == "Other"
    assert lm.get_sector("acme") == "Industrials"


@pytest.mark.parametrize("value, status", [
    (1_000, LearningModeStatus.PASS),
    (5_000, LearningModeStatus.WARN),
    (15_000, LearningModeStatus.TRIM_RECOMMEND),
    (25_000, LearningModeStatus.TRIM_AUTO),
])
def test_existing_position_ladder(lm, value, status):
    assert lm.check_existing_position("ACME", value, 100_000).status is status


def test_emergency_trim_targets_15pct(lm):
    v = lm.check_existing_position("ACME", 30_000, 100_000)
    assert v.trim_recommendation["target_pct"] == 15.0
    assert v.trim_recommendation["trim_value"] == 15_000.0


def test_risk_off_trims_deeper(lm):
    v = lm.check_existing_position("ACME", 20_000, 100_000, is_risk_off=True)
    assert v.trim_recommendation["target_pct"] == 10.0
    assert v.trim_recommendation["reason"] == "TRIM_RECOMMEND_RISK_OFF"


def test_new_trade_blocked_above_10pct(lm):
    v = lm.check_new_trade("ACME", "BUY", 6_000, 5_000, 100_000)
    assert v.status is LearningModeStatus.BLOCK


def test_new_trade_blocked_by_sector(lm):
    lm.calculate_sector_exposures([{"ticker": "FLUX", "value": 26_000}], 100_000)
    assert lm.check_new_trade("BOLT", "BUY", 1_000, 0, 100_000).status is LearningModeStatus.BLOCK


def test_exit_always_allowed(lm):
    assert lm.check_new_trade("ACME", "SELL", 99_000, 0, 100_000).status is LearningModeStatus.PASS


def test_scan_all_positions(lm):
    out = lm.scan_all_positions([{"ticker": "ACME", "value": 26_000}, {"ticker": "BOLT", "market_value": 6_000},
                                 {"ticker": "FLUX", "value": 1_000}], 100_000)
    assert out["summary"] == {"emergency_count": 1, "recommend_count": 0, "soft_warning_count": 1,
                              "sector_warning_count": 1}
    assert out["sector_warnings"][0]["sector"] == "Industrials"


def test_profit_milestones():
    assert ProfitMilestoneTracker.calculate_profit_pct(100, 112, "LONG") == pytest.approx(12)
    assert ProfitMilestoneTracker.calculate_profit_pct(100, 88, "short") == pytest.approx(12)
    assert ProfitMilestoneTracker.calculate_profit_pct(0, 88, "LONG") == 0.0
    assert ProfitMilestoneTracker.get_milestones_to_record(16, [5]) == [10, 15]
    assert ProfitMilestoneTracker.calculate_giveback(20, 12) == 8
    assert ProfitMilestoneTracker.calculate_giveback(20, 25) == 0
    assert ProfitMilestoneTracker.calculate_giveback(-1, -5) == 0
