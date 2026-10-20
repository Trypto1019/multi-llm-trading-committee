"""One full committee cycle: data -> council -> majority -> arbiter -> Risk Marshal -> paper intent.

Order of authority (each layer can only *narrow* what the previous allowed):

  0. deterministic pre-checks  health check, emergency trims, trailing stops
                               (no model is consulted; these run first)
  1. context agents            macro regime + per-ticker news (inform only)
  2. council                   3 models in parallel, strict 2-of-3 majority
  3. proposal                  deterministic; no majority -> no trade
  4. rule gates                tiered SKIP patterns over the trade context
  5. arbiter                   veto-only, whitelisted reasons, cannot flip
  6. Risk Marshal              pure Python, final, no appeal
  7. order intent              appended to a JSONL log; there is no broker code

Every ticker decision is written to the audit trail, including no-trades.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.base_agent import AgentError, CallRecord, JsonRoleAgent
from audit.audit_trail import AuditTrail
from committee.config import CommitteeConfig
from committee.roles import MACRO_SCHEMA, NEWS_SCHEMA
from consensus.arbiter import ARBITER_SCHEMA, ArbiterContext, data_blockers, rule
from consensus.council import OPINION_SCHEMA, Council, CouncilMember
from consensus.proposal import NoTrade, Proposal, build_proposal
from providers.base import Provider
from risk_marshal import LearningModeEnforcer, PortfolioState, RiskMarshal
from rules.rule_engine import RuleEngine, build_trade_context
from stops.trailing_stops import TrailingStopManager


@dataclass
class MarketSnapshot:
    as_of: str
    vix: float
    universe: list[str]
    portfolio: dict[str, Any]
    tickers: dict[str, dict[str, Any]]
    candidates: list[str]
    news: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: str | Path) -> "MarketSnapshot":
        d = Path(directory)
        market = json.loads((d / "market.json").read_text(encoding="utf-8"))
        news = json.loads((d / "news.json").read_text(encoding="utf-8"))
        return cls(news=news.get("headlines", {}), **{k: market[k] for k in (
            "as_of", "vix", "universe", "portfolio", "tickers", "candidates")})

    def portfolio_state(self) -> PortfolioState:
        p = self.portfolio
        positions = [{"symbol": x["ticker"], "market_value": x["market_value"],
                      "sector": self.tickers.get(x["ticker"], {}).get("sector")} for x in p["positions"]]
        sectors: dict[str, float] = {}
        for pos in positions:
            if pos["sector"]:
                sectors[pos["sector"]] = sectors.get(pos["sector"], 0.0) + pos["market_value"]
        corr = {(a, b): r for a, b, r in p.get("correlations", [])}
        return PortfolioState(
            nav=p["nav"], high_water_mark=p["high_water_mark"], cash=p["cash"], positions=positions,
            sectors=sectors, today_new_positions=p.get("today_new_positions", 0),
            today_deployed=p.get("today_deployed", 0.0), correlation_matrix=corr)


class OrderIntentLog:
    """Append-only JSONL of paper order intents. Nothing here talks to a broker."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.intents: list[dict[str, Any]] = []

    def write(self, intent: dict[str, Any]) -> dict[str, Any]:
        record = {"intent_id": uuid.uuid4().hex[:12], "mode": "PAPER_INTENT_ONLY",
                  "ts": datetime.now(timezone.utc).isoformat(), **intent}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        self.intents.append(record)
        return record


@dataclass
class TickerOutcome:
    ticker: str
    stage: str  # where it stopped: council | proposal | rules | arbiter | risk_marshal | intent
    result: str  # NO_TRADE | VETOED | RISK_FAIL | INTENT
    detail: str
    intent: dict[str, Any] | None = None


class CommitteeCycle:
    def __init__(self, provider: Provider, config: CommitteeConfig, out_dir: str | Path,
                 rules: RuleEngine | None = None) -> None:
        self.provider = provider
        self.config = config
        self.out_dir = Path(out_dir)
        self.cycle_id = datetime.now(timezone.utc).strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.calls: list[CallRecord] = []
        self.audit = AuditTrail(self.out_dir / "audit")
        self.intents = OrderIntentLog(self.out_dir / "order_intents.jsonl")
        self.marshal = RiskMarshal()
        self.rules = rules or RuleEngine()
        self.stops = TrailingStopManager()

        def agent(name: str, schema: dict[str, Any], model: str, prompt: str | None = None) -> JsonRoleAgent:
            return JsonRoleAgent(provider, agent_name=name, prompt_name=prompt, schema=schema, model_id=model,
                                 call_sink=self.calls.append, retry_delay=config.retry_delay)

        self.news_agent = agent("news_analyst", NEWS_SCHEMA, config.news_model)
        self.macro_agent = agent("macro_regime", MACRO_SCHEMA, config.macro_model)
        self.arbiter_agent = agent("arbiter", ARBITER_SCHEMA, config.arbiter_model)
        self.council = Council([
            CouncilMember(model.split("/")[-1], agent("council_member", OPINION_SCHEMA, model))
            for model in config.council_models
        ])

    # ---------------------------------------------------------- stage 0
    def pre_checks(self, snap: MarketSnapshot, state: PortfolioState) -> dict[str, Any]:
        """Deterministic work that happens before any model is asked anything."""
        health = self.marshal.validate_portfolio_health(state)
        sector_map = {t: v["sector"] for t, v in snap.tickers.items() if v.get("sector")}
        lm = LearningModeEnforcer(sector_map)
        scan = lm.scan_all_positions(
            [{"ticker": p["ticker"], "value": p["market_value"]} for p in snap.portfolio["positions"]], state.nav)
        trims = []
        for v in scan["emergency_trims"]:
            rec = v.trim_recommendation or {}
            px = snap.tickers[v.ticker]["last"]
            trims.append(self.intents.write({
                "cycle_id": self.cycle_id, "source": "emergency_trim", "ticker": v.ticker, "side": "SELL",
                "notional": rec.get("trim_value"), "quantity": math.floor(rec.get("trim_value", 0) / px),
                "limit_price": px, "reason": v.reason}))
        stop_actions = []
        for p in snap.portfolio["positions"]:
            t = snap.tickers[p["ticker"]]
            decision = self.stops.process_position(
                p["ticker"], p["entry"], t["last"], p["direction"], p.get("stop"), t.get("day_extreme"))
            stop_actions.append({"ticker": p["ticker"], **decision.to_dict()})
            if decision.action in ("PLACE_NEW", "UPDATE"):
                self.intents.write({
                    "cycle_id": self.cycle_id, "source": "trailing_stop", "ticker": p["ticker"],
                    "side": "STOP_" + ("SELL" if p["direction"] == "LONG" else "BUY"),
                    "stop_price": decision.new_stop, "reason": decision.reason})
        return {"health": health.to_dict(), "learning_mode": {
                    "summary": scan["summary"], "sector_exposure": scan["sector_exposure"]},
                "emergency_trims": trims, "trailing_stops": stop_actions}

    # ---------------------------------------------------------- stages 1-7
    async def run(self, snap: MarketSnapshot) -> dict[str, Any]:
        state = snap.portfolio_state()
        pre = self.pre_checks(snap, state)

        try:
            macro = await self.macro_agent.call(payload={"as_of": snap.as_of, "vix": snap.vix}, key="MARKET")
        except AgentError as e:
            macro = {"regime": "NEUTRAL", "vix": {"level": snap.vix}, "summary": f"macro unavailable: {e}"}

        outcomes = []
        for ticker in snap.candidates:
            outcomes.append(await self.decide(ticker, snap, state, macro))

        summary = {
            "as_of": snap.as_of,
            "pre_checks": pre,
            "macro": macro,
            "outcomes": [o.__dict__ for o in outcomes],
            "model_calls": [c.__dict__ for c in self.calls],
        }
        self.audit.save_cycle_summary(self.cycle_id, summary)
        return summary

    async def decide(self, ticker: str, snap: MarketSnapshot, state: PortfolioState,
                     macro: dict[str, Any]) -> TickerOutcome:
        mkt = snap.tickers[ticker]
        snapshot: dict[str, Any] = {"market": mkt, "macro": macro, "vix": snap.vix}

        def finish(outcome: TickerOutcome) -> TickerOutcome:
            snapshot["outcome"] = outcome.__dict__
            self.audit.save_decision(self.cycle_id, ticker, snapshot)
            return outcome

        # 1. news context (informs, never decides)
        try:
            news = await self.news_agent.call(payload={"ticker": ticker, "headlines": snap.news.get(ticker, [])},
                                              ticker=ticker)
        except AgentError as e:
            news = {"ticker": ticker, "sentiment": "NEUTRAL", "catalyst_flag": False, "headlines": [],
                    "summary": f"news unavailable: {type(e).__name__}"}
        snapshot["news"] = news

        # 2. council, in parallel
        payload = {"ticker": ticker, "last": mkt["last"], "bars": mkt["bars"], "news": news,
                   "macro_regime": macro.get("regime"), "vix": snap.vix}
        result = await self.council.deliberate(ticker, payload)
        snapshot["council"] = result.to_dict()

        # 3. deterministic proposal
        proposal = build_proposal(result, mkt["last"], sector=mkt.get("sector"))
        snapshot["proposal"] = proposal.to_dict()
        if isinstance(proposal, NoTrade):
            return finish(TickerOutcome(ticker, "proposal", "NO_TRADE", proposal.reason))

        # 4. rule gates
        ctx = build_trade_context(snapshot["council"], news, macro, mkt)
        skip, pattern = self.rules.should_skip_trade(ctx)
        snapshot["rule_context"] = ctx
        if skip and pattern:
            return finish(TickerOutcome(ticker, "rules", "NO_TRADE", f"SKIP pattern {pattern.pattern_id}"))

        # 5. arbiter (veto-only). Hard data blockers short-circuit the model call entirely.
        ruling = await self._arbitrate(proposal, result.to_dict(), news, macro, mkt)
        snapshot["arbiter"] = ruling.to_dict()
        if not ruling.approved:
            return finish(TickerOutcome(ticker, "arbiter", "VETOED",
                                        f"{ruling.veto_reason_code}: {ruling.veto_detail}"))

        # 6. Risk Marshal: final, deterministic
        verdict = self.marshal.validate_trade(proposal.to_trade_card(), state, snap.universe, vix=snap.vix)
        snapshot["risk_verdict"] = verdict.to_dict()
        if not verdict.executable or verdict.approved_size_pct is None:
            return finish(TickerOutcome(ticker, "risk_marshal", "RISK_FAIL", ", ".join(verdict.violation_codes())))

        # 7. paper order intent
        notional = round(verdict.approved_size_pct / 100 * state.nav, 2)
        intent = self.intents.write({
            "cycle_id": self.cycle_id, "source": "committee", "ticker": ticker, "side": proposal.action,
            "size_pct": verdict.approved_size_pct, "notional": notional,
            "quantity": math.floor(notional / proposal.entry), "limit_price": proposal.entry,
            "stop_loss": proposal.stop_loss, "take_profit": proposal.take_profit,
            "risk_status": verdict.status.value, "council_agreement": proposal.agreement})
        if proposal.action in ("BUY", "SHORT"):
            state.today_new_positions += 1
            state.today_deployed += notional
            if proposal.sector:
                state.sectors[proposal.sector] = state.sectors.get(proposal.sector, 0.0) + notional
        snapshot["order_intent"] = intent
        warn = f" ({'; '.join(verdict.warnings)})" if verdict.warnings else ""
        return finish(TickerOutcome(ticker, "intent", "INTENT", f"{proposal.action} {notional:.0f}{warn}", intent))

    async def _arbitrate(self, proposal: Proposal, council: dict[str, Any], news: dict[str, Any],
                         macro: dict[str, Any], mkt: dict[str, Any]):
        hard = data_blockers(mkt)
        if hard:
            return rule(proposal, None, hard)
        context = ArbiterContext(proposal=proposal.to_dict(), council=council, news=news, macro=macro,
                                 market={k: v for k, v in mkt.items() if k != "bars"})
        try:
            out = await self.arbiter_agent.call(payload=context.to_payload(), ticker=proposal.ticker)
            return rule(proposal, out, fail_closed=self.config.arbiter_fail_closed)
        except AgentError as e:
            return rule(proposal, None, arbiter_error=f"{type(e).__name__}: {e}",
                        fail_closed=self.config.arbiter_fail_closed)
