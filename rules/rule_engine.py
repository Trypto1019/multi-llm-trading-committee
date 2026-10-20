"""Safe trigger DSL and pattern rule engine.

Triggers are small boolean expressions over a nested context dict, e.g.

    agents_bullish >= 2 AND news.catalyst_flag == false
    macro.regime == 'RISK_OFF' OR (macro.vix > 30 AND NOT council.has_majority)

The expression is tokenized and parsed by a recursive-descent parser into a
tiny AST and evaluated directly. There is no `eval`, no attribute access, no
function calls, and no way to reach anything outside the context dict.

Grammar (precedence low -> high):
    expr    := or
    or      := and ("OR" and)*
    and     := not ("AND" not)*
    not     := "NOT" not | cmp
    cmp     := operand (OP operand)?
    operand := NUMBER | STRING | true | false | null | PATH | "(" expr ")"

Any error (unknown key, type mismatch, syntax) makes the trigger NOT match,
and the error is reported. Failing closed is the point.
"""

from __future__ import annotations

import json
import math
import operator
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# Tier thresholds: a pattern earns trust only with sample size and significance.
TIER_A_MIN_WINRATE = 0.60
TIER_A_MIN_SAMPLES = 30
TIER_A_MAX_P_VALUE = 0.05
TIER_B_MIN_WINRATE = 0.55
TIER_B_MIN_SAMPLES = 20
TIER_B_MAX_P_VALUE = 0.10

MAX_EXPRESSION_LENGTH = 500
MAX_DEPTH = 32

_OPS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt,
        "<=": operator.le, ">": operator.gt, ">=": operator.ge}

_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
    | (?P<str>'[^']*'|"[^"]*")
    | (?P<op>==|!=|<=|>=|<|>)
    | (?P<lp>\()
    | (?P<rp>\))
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    )""", re.VERBOSE)

_KEYWORDS = {"AND", "OR", "NOT"}
_LITERALS = {"true": True, "false": False, "null": None}


class TriggerSyntaxError(ValueError):
    pass


class TriggerEvalError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    kind: str  # num | str | op | lp | rp | kw | lit | path
    value: Any


def tokenize(expression: str) -> list[Token]:
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise TriggerSyntaxError(f"expression longer than {MAX_EXPRESSION_LENGTH} chars")
    tokens: list[Token] = []
    pos = 0
    text = expression.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise TriggerSyntaxError(f"unexpected character at {pos}: {text[pos:pos + 10]!r}")
        pos = m.end()
        kind = m.lastgroup
        raw = m.group(kind)
        if kind == "num":
            tokens.append(Token("num", float(raw) if "." in raw else int(raw)))
        elif kind == "str":
            tokens.append(Token("str", raw[1:-1]))
        elif kind == "word":
            if raw.upper() in _KEYWORDS:
                tokens.append(Token("kw", raw.upper()))
            elif raw.lower() in _LITERALS:
                tokens.append(Token("lit", _LITERALS[raw.lower()]))
            else:
                tokens.append(Token("path", raw))
        else:
            tokens.append(Token(kind, raw))
    return tokens


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.i = 0
        self.depth = 0

    def peek(self) -> Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise TriggerSyntaxError("unexpected end of expression")
        self.i += 1
        return tok

    def parse(self) -> tuple:
        if not self.tokens:
            raise TriggerSyntaxError("empty expression")
        node = self.parse_or()
        if self.peek() is not None:
            raise TriggerSyntaxError(f"unexpected token {self.peek().value!r}")
        return node

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise TriggerSyntaxError("expression nested too deeply")

    def parse_or(self) -> tuple:
        node = self.parse_and()
        while (t := self.peek()) and t.kind == "kw" and t.value == "OR":
            self.take()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> tuple:
        node = self.parse_not()
        while (t := self.peek()) and t.kind == "kw" and t.value == "AND":
            self.take()
            node = ("and", node, self.parse_not())
        return node

    def parse_not(self) -> tuple:
        t = self.peek()
        if t and t.kind == "kw" and t.value == "NOT":
            self.take()
            self._enter()
            node = ("not", self.parse_not())
            self.depth -= 1
            return node
        return self.parse_cmp()

    def parse_cmp(self) -> tuple:
        left = self.parse_operand()
        t = self.peek()
        if t and t.kind == "op":
            self.take()
            return ("cmp", t.value, left, self.parse_operand())
        return ("truthy", left)

    def parse_operand(self) -> tuple:
        t = self.take()
        if t.kind in ("num", "str", "lit"):
            return ("lit", t.value)
        if t.kind == "path":
            return ("path", t.value)
        if t.kind == "lp":
            self._enter()
            node = self.parse_or()
            self.depth -= 1
            if self.take().kind != "rp":
                raise TriggerSyntaxError("missing ')'")
            return node
        raise TriggerSyntaxError(f"unexpected token {t.value!r}")


def parse(expression: str) -> tuple:
    return _Parser(tokenize(expression)).parse()


def resolve_path(context: Mapping[str, Any], path: str) -> Any:
    value: Any = context
    for part in path.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            raise TriggerEvalError(f"context key not found: {path}")
    return value


def _eval(node: tuple, ctx: Mapping[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        return resolve_path(ctx, node[1])
    if kind == "and":
        return bool(_eval(node[1], ctx)) and bool(_eval(node[2], ctx))
    if kind == "or":
        return bool(_eval(node[1], ctx)) or bool(_eval(node[2], ctx))
    if kind == "not":
        return not bool(_eval(node[1], ctx))
    if kind == "truthy":
        return bool(_eval(node[1], ctx))
    if kind == "cmp":
        op, left, right = node[1], _eval(node[2], ctx), _eval(node[3], ctx)
        numeric = (int, float)
        if op in ("<", "<=", ">", ">="):
            if not (isinstance(left, numeric) and isinstance(right, numeric)) or \
                    isinstance(left, bool) or isinstance(right, bool):
                raise TriggerEvalError(f"cannot order {left!r} {op} {right!r}")
            if any(isinstance(v, float) and math.isnan(v) for v in (left, right)):
                raise TriggerEvalError("NaN in comparison")
        return _OPS[op](left, right)
    raise TriggerEvalError(f"unknown node {kind}")


def _paths(node: tuple) -> list[str]:
    if node[0] == "path":
        return [node[1]]
    return [p for child in node[1:] if isinstance(child, tuple) for p in _paths(child)]


@dataclass(frozen=True)
class TriggerResult:
    matched: bool
    expression: str
    context_snapshot: dict[str, Any]
    error: str | None = None


def evaluate(expression: str, context: Mapping[str, Any]) -> TriggerResult:
    """Evaluate one trigger. Never raises; errors yield matched=False."""
    try:
        ast = parse(expression)
    except TriggerSyntaxError as e:
        return TriggerResult(False, expression, {}, f"syntax: {e}")
    snapshot: dict[str, Any] = {}
    for p in _paths(ast):
        try:
            snapshot[p] = resolve_path(context, p)
        except TriggerEvalError:
            pass
    try:
        return TriggerResult(bool(_eval(ast, context)), expression, snapshot)
    except (TriggerEvalError, TypeError) as e:
        return TriggerResult(False, expression, snapshot, f"eval: {e}")


def evaluate_trigger(expression: str, context: Mapping[str, Any]) -> bool:
    return evaluate(expression, context).matched


# ----------------------------------------------------------------- patterns
@dataclass(frozen=True)
class PatternMatch:
    pattern_id: str
    trigger: str
    tier: str
    action: str  # TRADE | SKIP
    historical_winrate: float | None
    live_winrate: float | None
    sample_size: int


@dataclass
class RuleEngine:
    """Holds named patterns ({id: {trigger, action, tier, live_stats, ...}})."""

    patterns: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> "RuleEngine":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(patterns=data.get("patterns", {}), path=p)

    def find_matching_patterns(self, context: Mapping[str, Any]) -> list[PatternMatch]:
        matches: list[PatternMatch] = []
        for pid, pattern in self.patterns.items():
            trigger = pattern.get("trigger", "")
            if not trigger or not evaluate(trigger, context).matched:
                continue
            live = pattern.get("live_stats", {})
            wins, losses = live.get("wins", 0), live.get("losses", 0)
            total = wins + losses
            matches.append(PatternMatch(
                pattern_id=pid,
                trigger=trigger,
                tier=pattern.get("tier", "C"),
                action=pattern.get("action", "TRADE"),
                historical_winrate=pattern.get("historical_winrate"),
                live_winrate=wins / total if total else None,
                sample_size=total,
            ))
        return matches

    @staticmethod
    def _tier_rank(tier: str) -> int:
        return {"A": 0, "B": 1, "C": 2}.get(tier, 3)

    def get_best_pattern(self, context: Mapping[str, Any]) -> PatternMatch | None:
        trade = [m for m in self.find_matching_patterns(context) if m.action != "SKIP"]
        if not trade:
            return None
        trade.sort(key=lambda m: (self._tier_rank(m.tier), -m.sample_size,
                                  -(m.live_winrate or m.historical_winrate or 0)))
        return trade[0]

    def should_skip_trade(self, context: Mapping[str, Any]) -> tuple[bool, PatternMatch | None]:
        skips = [m for m in self.find_matching_patterns(context) if m.action == "SKIP"]
        if not skips:
            return False, None
        skips.sort(key=lambda m: self._tier_rank(m.tier))
        return True, skips[0]

    def update_live_stats(self, pattern_id: str, won: bool, return_pct: float,
                          today: date | None = None) -> bool:
        pattern = self.patterns.get(pattern_id)
        if pattern is None:
            return False
        live = pattern.setdefault("live_stats", {"wins": 0, "losses": 0, "total_return_pct": 0.0})
        live["wins" if won else "losses"] = live.get("wins" if won else "losses", 0) + 1
        live["total_return_pct"] = live.get("total_return_pct", 0.0) + return_pct
        total = live["wins"] + live["losses"]
        live["avg_return_pct"] = live["total_return_pct"] / total
        live["last_updated"] = (today or date.today()).isoformat()
        pattern["tier"] = calculate_tier(live, pattern.get("p_value"))
        return True

    def save(self) -> None:
        if self.path is None:
            raise ValueError("no path to save to")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"patterns": self.patterns}, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def calculate_tier(live_stats: Mapping[str, Any], p_value: float | None) -> str:
    """A: WR>=60%, N>=30, p<0.05.  B: WR>=55%, N>=20, p<0.10.  Else C.

    A missing p-value is NOT treated as significant: no test, no tier.
    """
    wins, losses = live_stats.get("wins", 0), live_stats.get("losses", 0)
    total = wins + losses
    if total == 0 or p_value is None:
        return "C"
    wr = wins / total
    if total >= TIER_A_MIN_SAMPLES and wr >= TIER_A_MIN_WINRATE and p_value < TIER_A_MAX_P_VALUE:
        return "A"
    if total >= TIER_B_MIN_SAMPLES and wr >= TIER_B_MIN_WINRATE and p_value < TIER_B_MAX_P_VALUE:
        return "B"
    return "C"


def build_trade_context(council: Mapping[str, Any], news: Mapping[str, Any] | None,
                        macro: Mapping[str, Any] | None, market: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten committee outputs into the context triggers are written against."""
    directions = council["consensus"]["all_directions"]
    members = {o["member"]: {"direction": o.get("direction") or "ABSTAIN",
                             "confidence": o.get("confidence") or 0.0}
               for o in council["opinions"]}
    return {
        "agents_bullish": sum(d == "BULLISH" for d in directions),
        "agents_bearish": sum(d == "BEARISH" for d in directions),
        "agents_neutral": sum(d == "NEUTRAL" for d in directions),
        "council": {"direction": council["consensus"]["direction"],
                    "has_majority": council["consensus"]["direction"] != "MIXED",
                    "abstentions": council["consensus"]["abstentions"]},
        "members": members,
        "news": {"sentiment": (news or {}).get("sentiment", "NEUTRAL"),
                 "catalyst_flag": bool((news or {}).get("catalyst_flag", False))},
        "macro": {"regime": (macro or {}).get("regime", "NEUTRAL"),
                  "vix": float((macro or {}).get("vix", {}).get("level", 0.0))},
        "market": {"r_multiple": market.get("r_multiple"), "rel_volume": market.get("rel_volume")},
    }
