"""Safe (no-eval) trigger DSL and tiered pattern engine."""

from rules.rule_engine import (
    PatternMatch,
    RuleEngine,
    TriggerResult,
    TriggerSyntaxError,
    build_trade_context,
    calculate_tier,
    evaluate,
    evaluate_trigger,
    parse,
)

__all__ = [
    "PatternMatch",
    "RuleEngine",
    "TriggerResult",
    "TriggerSyntaxError",
    "build_trade_context",
    "calculate_tier",
    "evaluate",
    "evaluate_trigger",
    "parse",
]
