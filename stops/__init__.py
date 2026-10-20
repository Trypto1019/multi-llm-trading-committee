"""Giveback trailing stops: tighten-only, buffered, idempotent."""

from stops.trailing_stops import (
    GIVEBACK_THRESHOLDS,
    InMemoryWatermarkStore,
    StopDecision,
    TrailingStopManager,
    calculate_trailing_stop,
    get_giveback_limit,
    should_update_stop,
)

__all__ = [
    "GIVEBACK_THRESHOLDS",
    "InMemoryWatermarkStore",
    "StopDecision",
    "TrailingStopManager",
    "calculate_trailing_stop",
    "get_giveback_limit",
    "should_update_stop",
]
