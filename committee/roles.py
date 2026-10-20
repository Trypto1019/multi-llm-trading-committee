"""Role registry: each role = a prompt file in prompts/ + an output schema.

The demo cycle uses news_analyst, macro_regime, council_member and arbiter.
equity_trader and options_trader are the sequential-variant roles (equity
plan -> options expression -> arbiter); their schemas are kept here so the
prompts and contracts stay in sync and are linted by the test suite.
"""

from __future__ import annotations

from typing import Any

from consensus.arbiter import ARBITER_SCHEMA
from consensus.council import OPINION_SCHEMA

NEWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ticker", "sentiment", "catalyst_flag", "headlines", "summary"],
    "properties": {
        "ticker": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["TAILWIND", "HEADWIND", "NEUTRAL", "MIXED"]},
        "catalyst_flag": {"type": "boolean"},
        "headlines": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "required": ["source", "headline", "impact", "relevance"],
            "properties": {
                "source": {"type": "string"}, "headline": {"type": "string"},
                "impact": {"type": "string", "enum": ["TAILWIND", "HEADWIND", "NEUTRAL", "CATALYST"]},
                "relevance": {"type": "number", "minimum": 0, "maximum": 1}}}},
        "summary": {"type": "string"},
        "notes_for_traders": {"type": "string"},
    },
}

MACRO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["regime", "confidence", "vix", "regime_permissions", "summary"],
    "properties": {
        "regime": {"type": "string", "enum": ["RISK_ON", "RISK_OFF", "NEUTRAL"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "vix": {"type": "object", "required": ["level"], "properties": {
            "level": {"type": "number", "minimum": 0},
            "term_structure": {"type": "string", "enum": ["CONTANGO", "BACKWARDATION", "FLAT"]},
            "interpretation": {"type": "string"}}},
        "favored_strategies": {"type": "array", "items": {"type": "string"}},
        "avoid_strategies": {"type": "array", "items": {"type": "string"}},
        "regime_permissions": {"type": "object", "properties": {
            "allow_momentum_long": {"type": "boolean"},
            "allow_breakout_long": {"type": "boolean"},
            "allow_mean_reversion_short": {"type": "boolean"},
            "allow_countertrend": {"type": "boolean"},
            "max_risk_multiplier": {"type": "number", "minimum": 0, "maximum": 1}}},
        "macro_risks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

EQUITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ticker", "direction", "action", "plan_type", "confidence", "validation"],
    "properties": {
        "ticker": {"type": "string"},
        "direction": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "action": {"type": "string", "enum": ["BUY", "SHORT", "ADD", "HOLD"]},
        "plan_type": {"type": "string", "enum": ["TRADE_NOW", "WAIT_FOR_TRIGGER", "NO_SETUP"]},
        "trade_plan": {"type": ["object", "null"], "properties": {
            "entry": {"type": "number"}, "stop": {"type": "number"},
            "target": {"type": "number"}, "r_multiple": {"type": "number"}}},
        "trigger_levels": {"type": ["object", "null"]},
        "what_to_watch": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "validation": {"type": "object", "required": ["r_valid", "structure_valid"]},
    },
}

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ticker", "options_edge", "edge_state", "recommended_strategy", "execution"],
    "properties": {
        "ticker": {"type": "string"},
        "options_edge": {"type": "boolean"},
        "edge_state": {"type": "string", "enum": ["NO_EDGE_NOW", "EDGE_IF_TRIGGER", "EDGE_PRESENT"]},
        "recommended_strategy": {"type": "string", "enum": [
            "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_CALL", "LONG_PUT",
            "IRON_CONDOR", "CALENDAR", "NO_TRADE"]},
        "execution": {"type": "object", "required": ["blocked"]},
        "unblock_conditions": {"type": "array", "items": {"type": "string"}},
    },
}

ROLES: dict[str, dict[str, Any]] = {
    "news_analyst": NEWS_SCHEMA,
    "macro_regime": MACRO_SCHEMA,
    "council_member": OPINION_SCHEMA,
    "equity_trader": EQUITY_SCHEMA,
    "options_trader": OPTIONS_SCHEMA,
    "arbiter": ARBITER_SCHEMA,
}
