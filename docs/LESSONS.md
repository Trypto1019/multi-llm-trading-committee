# Lessons

This repository is a clean rebuild of a system that paper-traded US equities
and options with a multi-model committee. Paper trading means a broker paper
account: no live capital was ever at risk. **No alpha is claimed.** The point of
this repo is the engineering, and that includes what the numbers said.

## 1. The honest results

Over about four weeks of autonomous paper trading:

| Metric | Result |
|---|---|
| Paper NAV change | **-0.95%** |
| Maximum drawdown | **-7%** |
| Closed trades analysed | **205** |
| Win rate | **35%** |
| Break-even win rate at the realised payoff ratio | **46.8%** |

A 35% win rate against a 46.8% break-even is a losing strategy. The pipeline
worked: it ran on schedule, the risk rails held, every decision was logged and
reviewable. It did not have an edge, and the system's own measurements are what
showed that.

## 2. The backtests were measuring the simulator

Before paper trading began, the original backtests looked excellent. A
self-audit then found why: they were Monte Carlo simulations with
**pre-assigned edges**. Price paths were synthetic, signal inputs (implied
volatility rank, dealer gamma, options flow) were randomly generated, and each
strategy was given a hard-coded drift. The backtest assumed the strategies
worked and then simulated that assumption.

The audit's conclusion, kept verbatim in spirit: the original backtests were
**essentially meaningless**. They tested whether the simulation code ran, not
whether the strategies worked.

Re-run on real historical prices, the picture flipped:

- A strategy that followed delayed public trade disclosures **lost money**. By the
  time a disclosure is public, the information is already in the price.
- A high-implied-volatility put-spread strategy kept a very high win rate and
  **still lost money**, because the average loss was several times the average
  win. Win rate alone says nothing about expectancy.
- Several signal families **could not be backtested at all**, because the vendor
  only exposed current snapshots, not history. Anything built on them was
  untestable by construction.

What changed afterwards:

1. **No strategy deploys on simulated results.** Real historical data or nothing.
2. **Payoff ratio is reported next to win rate, always**, with the break-even
   win rate it implies.
3. **Data availability is checked before strategy design.** If a signal has no
   history, record it daily first, then decide.
4. **Optimised factor results are presumed overfit** until reproduced out of
   sample. A very high in-sample Sharpe ratio is a warning sign, not a headline.
   This repo deliberately quotes none.

## 3. Why deterministic risk code sits *after* the models

Language models are good at reading context and bad at being a hard
constraint. In production, the failure modes were consistent:

- **Persuasion drift.** A fluent rationale can talk a downstream model into
  "just this once". A Python `if` statement cannot be talked into anything.
- **Format fragility.** Models wrapped JSON in prose, invented fields, or mixed up
  similar actions (a COVER/SHORT misparse was one of the early bug fixes). Every
  output is therefore schema-validated, and every action is normalised by code.
- **Authority creep.** An arbiter asked to "check the trade" starts re-deciding the
  trade. Here the arbiter's authority is enforced in code: approve or veto from a
  whitelist, never flip direction. Overreach is stripped and logged.
- **Correlated confidence.** Three models given the same prompt can be confidently
  wrong together. A majority vote reduces idiosyncratic noise; it does nothing
  for shared blind spots. That is why the vote is followed by limits that do not
  care how confident anyone was.

So the order is fixed: models propose, a deterministic proposal builder
normalises, an arbiter can only narrow, and the **Risk Marshal**, a pure-Python
module with immutable constants and no configuration surface, has the final
word. Emergency trims run before any model is consulted at all.

## 4. Operational lessons

- **Measure the system against itself.** The per-decision audit trail (every
  input, every opinion, the ruling, the verdict) is what made the results
  analysable. Without it the numbers in section 1 would be anecdotes.
- **An abstention is not a vote.** When a provider fails, the council does not
  shrink its denominator. Two survivors must agree, or nothing happens.
- **Fail closed where it is cheap, fail open where it is not.** A failed arbiter
  call means HOLD: a missed trade costs nothing. In a sibling research project, a
  billing error that failed closed blocked a trade the rules wanted, which
  is the correct trade-off for a system with no demonstrated edge.
- **Stops only tighten.** Giveback-based trailing stops lock in part of an open
  gain; the watermark is persisted and the update is idempotent, so a re-run
  cannot loosen anything.
- **Separate "rank" from "predict".** The debate ranker takes a blind
  probability before showing the market price. A price-anchored score mostly
  re-derives the market's own number and tells you nothing new.

## 5. Preregistration and killing ideas

The debate-ranker experiment in the sibling project was preregistered: fixed
pools that were never pooled, a fixed review sample size, a success criterion
written down before the data arrived (rank correlation between score and
outcome), and a non-binding interim look that could not promote or retire
anything. The same discipline applies here. An idea that does not clear its
preregistered bar is retired, however good the story sounded.
