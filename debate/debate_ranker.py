"""Multi-LLM debate ranker for binary trade ideas (paper research only).

Question it answers: given a set of candidate ideas the system already
generated, can a model panel *rank* them better than the market price does?

Pipeline per candidate (all before the event window opens):

1. RESEARCH  a neutral briefing; the model is told not to pick a side.
2. ADVOCATE  two different models argue opposite outcomes, concurrently.
             Which model argues which side is decided by a hash of the case
             id: stable on re-runs, balanced across the sample, and recorded,
             so a "one model is just the better debater" bias is measurable.
3. BLIND     a judge reads the briefing and both cases WITHOUT the price and
             returns a probability for the backed outcome.
4. PRICED    the same judge then sees the market-implied probability and its
             own blind number and returns a 1-10 "good bet at this price" score.

The blind-then-priced split matters: a price-anchored number mostly re-derives
the market's own probability. Taking the blind estimate first lets it be
scored against the market independently.

Leak guard: a candidate whose event window has already opened is refused, so
the research step cannot "predict" an outcome it can look up.

There is no code path from this module to an order.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.base_agent import AgentCallError, BaseAgent
from providers.base import Message, Provider, ProviderError


class LeakGuardError(RuntimeError):
    """Refused: the event window has opened; a score now would be a recap, not a prediction."""


@dataclass(frozen=True)
class Candidate:
    case_id: str
    question: str  # e.g. "Does ACME close the week above 52.00?"
    outcome_backed: str  # the outcome the idea is betting on
    outcome_other: str
    price: float  # market-implied probability of `outcome_backed`, 0 < p < 1
    window_opens_at: datetime  # leak-guard cutoff
    context: str = ""  # optional extra facts (synthetic in the demo)

    def __post_init__(self) -> None:
        if not 0 < self.price < 1:
            raise ValueError("price must be a probability in (0, 1)")
        if self.window_opens_at.tzinfo is None:
            raise ValueError("window_opens_at must be timezone-aware")


@dataclass(frozen=True)
class DebateConfig:
    research_model: str = "perplexity/sonar-pro"
    advocates: tuple[str, str] = ("anthropic/claude-sonnet-4.5", "openai/gpt-4o")
    judge_model: str = "anthropic/claude-sonnet-4.5"


def assign_sides(case_id: str, advocates: tuple[str, str]) -> tuple[str, str]:
    """(advocate_for_backed, advocate_for_other), decided by a hash of the case id."""
    h = hashlib.sha256(case_id.encode()).digest()[0]
    return (advocates[0], advocates[1]) if h % 2 == 0 else (advocates[1], advocates[0])


# ------------------------------------------------------------------ agents
class _TextStep:
    """Free-text model step (research, advocacy) with typed errors."""

    def __init__(self, provider: Provider, agent: str) -> None:
        self.provider, self.agent = provider, agent

    async def run(self, model: str, prompt: str, key: str, max_tokens: int = 800) -> str:
        try:
            c = await self.provider.complete(model, [{"role": "user", "content": prompt}],
                                             max_tokens=max_tokens, metadata={"agent": self.agent, "key": key})
        except ProviderError as e:
            raise AgentCallError(f"{self.agent}/{model}: {e}") from e
        if not c.text.strip():
            raise AgentCallError(f"{self.agent}/{model}: empty response")
        return c.text.strip()


class _JudgeStep(BaseAgent):
    def __init__(self, provider: Provider, agent_name: str, schema: dict[str, Any], model_id: str,
                 **kw: Any) -> None:
        self.agent_name = agent_name
        self._schema = schema
        super().__init__(provider, model_id=model_id, **kw)

    @property
    def output_schema(self) -> dict[str, Any]:
        return self._schema

    def build_messages(self, *, prompt: str, **_: Any) -> list[Message]:
        return [{"role": "user", "content": prompt + "\n\nRespond with ONLY a JSON object matching: "
                 + json.dumps(self._schema, sort_keys=True)}]


BLIND_SCHEMA = {
    "type": "object", "required": ["p_backed", "key_factor"],
    "properties": {"p_backed": {"type": "number", "minimum": 0, "maximum": 1},
                   "key_factor": {"type": "string", "minLength": 1}},
}
PRICED_SCHEMA = {
    "type": "object", "required": ["score", "rationale"],
    "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 10},
                   "rationale": {"type": "string", "minLength": 1}},
}


# ---------------------------------------------------------------- prompts
def research_prompt(c: Candidate) -> str:
    return (
        f"Give a neutral, factual briefing relevant to this question: {c.question}\n"
        f"{c.context}\n\n"
        "Rules: do NOT pick a side, predict an outcome or give trading advice. Report only "
        "information available before the event window opens; if you encounter the outcome, "
        "ignore it and do not mention it. Be concise."
    )


def advocate_prompt(c: Candidate, outcome: str, brief: str) -> str:
    return (
        f"QUESTION: {c.question}\n\nNEUTRAL BRIEFING:\n{brief}\n\n"
        f"Make the strongest honest case for this outcome: {outcome}.\n"
        "Argue as well as the evidence genuinely allows, but do not invent facts; if the case "
        "is weak, say so plainly. An advocate who oversells a bad case makes the panel worse. "
        "200 words maximum."
    )


def blind_prompt(c: Candidate, brief: str, case_backed: str, case_other: str) -> str:
    return (
        f"QUESTION: {c.question}\n\nNEUTRAL BRIEFING:\n{brief}\n\n"
        f"CASE FOR '{c.outcome_backed}':\n{case_backed}\n\n"
        f"CASE FOR '{c.outcome_other}':\n{case_other}\n\n"
        f"Weigh both cases against the briefing and estimate the probability of '{c.outcome_backed}'. "
        "You have NOT been shown any market price; estimate from the evidence alone. Be calibrated, "
        "not bold: a number far from 0.5 needs evidence that justifies it."
    )


def priced_prompt(c: Candidate, brief: str, case_backed: str, case_other: str,
                  blind: dict[str, Any]) -> str:
    return (
        f"QUESTION: {c.question}\n\nNEUTRAL BRIEFING:\n{brief}\n\n"
        f"CASE FOR '{c.outcome_backed}':\n{case_backed}\n\nCASE FOR '{c.outcome_other}':\n{case_other}\n\n"
        f"YOUR OWN BLIND ESTIMATE (made before you saw any price): {blind['p_backed']:.2f}. "
        f"Your key factor was: {blind['key_factor']}\n\n"
        f"THE MARKET implies a probability of {c.price:.2f} for '{c.outcome_backed}'.\n\n"
        "Score 1-10 how confident you are that backing this outcome AT THIS PRICE is a good idea. "
        "This is about price versus probability, not about what is likely. Use the full range; a "
        "score that never leaves 4-7 carries no information. Do not assume the market is right."
    )


# --------------------------------------------------------------- pipeline
class DebateRanker:
    def __init__(self, provider: Provider, config: DebateConfig | None = None,
                 retry_delay: float = 0.0) -> None:
        self.config = config or DebateConfig()
        self.research = _TextStep(provider, "debate_research")
        self.advocate = _TextStep(provider, "debate_advocate")
        self.blind = _JudgeStep(provider, "debate_blind_judge", BLIND_SCHEMA, self.config.judge_model,
                                retry_delay=retry_delay)
        self.priced = _JudgeStep(provider, "debate_priced_judge", PRICED_SCHEMA, self.config.judge_model,
                                 retry_delay=retry_delay)

    async def score(self, c: Candidate, as_of: datetime | None = None) -> dict[str, Any]:
        now = as_of or datetime.now(timezone.utc)
        if now >= c.window_opens_at:
            raise LeakGuardError(f"{c.case_id}: event window opened at {c.window_opens_at.isoformat()}")

        adv_backed, adv_other = assign_sides(c.case_id, self.config.advocates)
        brief = await self.research.run(self.config.research_model, research_prompt(c), c.case_id)
        case_backed, case_other = await asyncio.gather(
            self.advocate.run(adv_backed, advocate_prompt(c, c.outcome_backed, brief), f"{c.case_id}:backed"),
            self.advocate.run(adv_other, advocate_prompt(c, c.outcome_other, brief), f"{c.case_id}:other"),
        )
        blind = await self.blind.call(prompt=blind_prompt(c, brief, case_backed, case_other), key=c.case_id)
        priced = await self.priced.call(
            prompt=priced_prompt(c, brief, case_backed, case_other, blind), key=c.case_id)

        return {
            "ts": now.isoformat(),
            "case_id": c.case_id,
            "question": c.question,
            "outcome_backed": c.outcome_backed,
            "price": c.price,
            "advocate_backed": adv_backed,
            "advocate_other": adv_other,
            "blind_p_backed": float(blind["p_backed"]),
            "blind_key_factor": blind["key_factor"],
            "blind_edge": round(float(blind["p_backed"]) - c.price, 4),
            "score": int(priced["score"]),
            "rationale": priced["rationale"],
            "research": brief,
            "case_backed": case_backed,
            "case_other": case_other,
            "outcome": None,  # filled in by grading, never by the ranker
            "note": "PAPER RESEARCH ONLY - scored before the event window; no order path.",
        }

    async def score_many(self, candidates: Sequence[Candidate], as_of: datetime | None = None
                         ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Score each candidate; leak-guarded or failed ones are logged, never silently dropped."""
        scored, skipped = [], []
        for c in candidates:
            try:
                scored.append(await self.score(c, as_of))
            except LeakGuardError as e:
                skipped.append({"case_id": c.case_id, "reason": f"leak_guard: {e}"})
            except AgentCallError as e:
                skipped.append({"case_id": c.case_id, "reason": f"call_error: {e}"})
        return scored, skipped


# ----------------------------------------------------------------- ledger
class DebateLedger:
    """Append-only JSONL. One score per case id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, record: dict[str, Any]) -> bool:
        if any(r["case_id"] == record["case_id"] for r in self.load()):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        return True


# ----------------------------------------------------------------- report
def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation with average ranks for ties. Stdlib only."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                rk[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return rk

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return None if den == 0 else num / den


def brier(probs: Iterable[float], outcomes: Iterable[int]) -> float | None:
    pairs = list(zip(probs, outcomes))
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def report(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pre-registered metrics on graded records (outcome 1 = backed outcome happened)."""
    graded = [r for r in records if r.get("outcome") in (0, 1)]
    out: dict[str, Any] = {"n_scored": len(records), "n_graded": len(graded)}
    if not graded:
        return out
    outcomes = [int(r["outcome"]) for r in graded]
    out["score_vs_outcome_spearman"] = spearman([r["score"] for r in graded], outcomes)
    out["brier_blind"] = brier([r["blind_p_backed"] for r in graded], outcomes)
    out["brier_market"] = brier([r["price"] for r in graded], outcomes)
    by_adv: dict[str, list[float]] = {}
    for r in graded:
        by_adv.setdefault(r["advocate_backed"], []).append(r["blind_p_backed"])
    out["mean_blind_p_by_backed_advocate"] = {k: round(sum(v) / len(v), 4) for k, v in by_adv.items()}
    return out
