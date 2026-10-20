from __future__ import annotations

import pytest

from stops import TrailingStopManager, calculate_trailing_stop, get_giveback_limit, should_update_stop


@pytest.mark.parametrize("profit, limit", [(10, None), (14.99, None), (15, 0.35), (24.9, 0.35),
                                           (25, 0.25), (34.9, 0.25), (35, 0.20), (80, 0.20)])
def test_giveback_tiers(profit, limit):
    assert get_giveback_limit(profit) == limit


def test_long_30pct_profit():
    c = calculate_trailing_stop(entry=100, best_price=130, current_price=130, direction="LONG")
    assert c.eligible and c.giveback_limit == 0.25
    assert c.stop_price == 122.5  # 130 - 30 * 0.25


def test_short_profit():
    c = calculate_trailing_stop(entry=10.0, best_price=7.5, current_price=7.5, direction="SHORT")
    assert c.eligible and c.giveback_limit == 0.25  # 25% profit
    assert c.stop_price == 8.12  # 7.5 + 2.5 * 0.25 = 8.125 -> 8.12


def test_not_eligible_below_threshold():
    c = calculate_trailing_stop(100, 112, 110, "LONG")
    assert not c.eligible and c.stop_price is None


def test_stop_never_inside_buffer_of_current_price():
    # best far above current: raw stop would be above the current price
    c = calculate_trailing_stop(entry=100, best_price=200, current_price=116, direction="LONG")
    assert c.stop_price <= round(116 * 0.998, 2)


def test_breakeven_clamp_long():
    c = calculate_trailing_stop(entry=100, best_price=116, current_price=116, direction="LONG")
    assert c.stop_price >= 100.5


def test_should_update_only_when_tighter():
    assert should_update_stop(100, 101, "LONG")[0]
    assert not should_update_stop(100, 99, "LONG")[0]
    assert should_update_stop(100, 99, "SHORT")[0]
    assert not should_update_stop(100, 101, "SHORT")[0]


def test_should_update_requires_minimum_move():
    ok, why = should_update_stop(100.0, 100.2, "LONG")  # 0.2 < 0.3 (0.3% of 100)
    assert not ok and "min" in why


def test_invalid_direction():
    with pytest.raises(ValueError):
        calculate_trailing_stop(100, 120, 120, "SIDEWAYS")


def test_process_position_flow_and_idempotency():
    m = TrailingStopManager()
    d1 = m.process_position("P1", entry=100, current_price=130, direction="LONG")
    assert d1.action == "PLACE_NEW" and d1.new_stop == 122.5
    d2 = m.process_position("P1", entry=100, current_price=130, direction="LONG", current_stop=d1.new_stop)
    assert d2.action == "KEEP"  # same inputs: no-op
    d3 = m.process_position("P1", entry=100, current_price=140, direction="LONG", current_stop=d1.new_stop)
    assert d3.action == "UPDATE" and d3.new_stop > d1.new_stop


def test_watermark_is_monotonic_and_stop_never_loosens():
    m = TrailingStopManager()
    m.process_position("P", 100, 140, "LONG")
    stop = m.process_position("P", 100, 140, "LONG").new_stop  # PLACE_NEW again (no current stop passed)
    pullback = m.process_position("P", 100, 128, "LONG", current_stop=stop)
    assert pullback.best_price == 140  # watermark kept
    assert pullback.action == "KEEP"   # a pullback never loosens the stop


def test_intraday_extreme_feeds_watermark():
    m = TrailingStopManager()
    d = m.process_position("P", 40.0, 47.2, "LONG", current_stop=38.5, intraday_best=48.1)
    assert d.best_price == 48.1 and d.action == "UPDATE" and d.new_stop == 45.27


def test_short_watermark_tracks_lows():
    m = TrailingStopManager()
    m.process_position("S", 10.0, 7.5, "SHORT")
    d = m.process_position("S", 10.0, 7.9, "SHORT")
    assert d.best_price == 7.5
