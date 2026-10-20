#!/usr/bin/env python3
"""Run one full committee cycle on synthetic data.

    python demo.py --mock            # offline: canned model outputs, no keys, no network
    python demo.py --mock --debate   # also run the debate ranker on synthetic ideas
    python demo.py                   # live models via OPENROUTER_API_KEY (still paper-only)

Outputs go to ./out/ (git-ignored): order_intents.jsonl and audit/<date>/<cycle>/*.json.
There is no broker integration anywhere in this repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from committee import CommitteeConfig, CommitteeCycle, MarketSnapshot
from debate import Candidate, DebateLedger, DebateRanker
from providers import ProviderError, get_provider
from rules import RuleEngine

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "fake_market"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _print_cycle(summary: dict, cycle: CommitteeCycle) -> None:
    pre = summary["pre_checks"]
    print(f"\n== Committee cycle {cycle.cycle_id}  (as of {summary['as_of']}, PAPER ONLY) ==")
    print("\n[0] Deterministic pre-checks (no model consulted)")
    print(f"    health: drawdown {pre['health']['drawdown_pct']}% "
          f"({pre['health']['drawdown_mode']}), issues: {len(pre['health']['issues'])}")
    for t in pre["emergency_trims"]:
        print(f"    emergency trim: SELL {t['ticker']} notional {t['notional']:.0f}  ({t['reason']})")
    for s in pre["trailing_stops"]:
        print(f"    trailing stop {s['ticker']}: {s['action']} {s['new_stop'] or ''}  ({s['reason']})")
    print(f"\n[1] Macro regime: {summary['macro'].get('regime')}  - {summary['macro'].get('summary')}")
    print("\n[2-7] Per-ticker decisions")
    print(f"    {'ticker':<6} {'stopped at':<13} {'result':<10} detail")
    for o in summary["outcomes"]:
        print(f"    {o['ticker']:<6} {o['stage']:<13} {o['result']:<10} {o['detail']}")
    calls = summary["model_calls"]
    retried = sum(1 for c in calls if c["attempts"] > 1)
    failed = sum(1 for c in calls if not c["success"])
    print(f"\n    model calls: {len(calls)} (retried: {retried}, failed after retries: {failed})")
    print(f"    order intents: {_rel(cycle.intents.path)}  ({len(cycle.intents.intents)} written)")
    print(f"    audit trail:   {_rel(cycle.audit.cycle_dir(cycle.cycle_id))}/")


async def _debate(provider, out: Path) -> None:
    raw = json.loads((DATA / "debate_candidates.json").read_text(encoding="utf-8"))["candidates"]
    cands = [Candidate(**{**c, "window_opens_at": datetime.fromisoformat(c["window_opens_at"])}) for c in raw]
    as_of = datetime.fromisoformat("2026-03-06T20:00:00+00:00")
    scored, skipped = await DebateRanker(provider).score_many(cands, as_of=as_of)
    ledger = DebateLedger(out / "debate_ledger.jsonl")
    print("\n== Debate ranker (research -> side-randomized advocates -> blind judge -> priced judge) ==")
    for r in scored:
        ledger.append(r)
        print(f"    {r['case_id']:<16} blind p={r['blind_p_backed']:.2f} vs market {r['price']:.2f} "
              f"(edge {r['blind_edge']:+.2f}) score {r['score']}/10  backed-side advocate: {r['advocate_backed']}")
    for s in skipped:
        print(f"    {s['case_id']:<16} SKIPPED  {s['reason']}")


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="use canned responses (offline, no keys)")
    ap.add_argument("--debate", action="store_true", help="also run the debate ranker demo")
    ap.add_argument("--out", default=str(ROOT / "out"), help="output directory (default ./out)")
    args = ap.parse_args(argv)

    try:
        provider = get_provider(mock=args.mock)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = Path(args.out)
    cycle = CommitteeCycle(provider, CommitteeConfig.load(mock=args.mock), out,
                           rules=RuleEngine.from_file(ROOT / "rules" / "sample_patterns.json"))
    summary = await cycle.run(MarketSnapshot.load(DATA))
    _print_cycle(summary, cycle)
    if args.debate:
        await _debate(provider, out)
    print("\nPaper-traded engineering demo. No live capital, no broker, no alpha claimed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
