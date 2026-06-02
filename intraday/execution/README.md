# `intraday/execution/` — paper ledger (NO live orders)

**PAPER ONLY.** No broker integration, no order routing, no live trades. This
is a hard guardrail until an edge is proven and live trading is a separate,
explicit decision with its own design.

- **Paper ledger:** log each gated signal as if filled, then mark-to-market on
  subsequent ticks. Produce the **same record shape as the backtest** so
  live-vs-backtest divergence is measurable.
- A broker adapter (Alpaca / IBKR) is **out of scope** here and must not be
  added without sign-off.

See `../DESIGN.md` §8. Tasks: `../TASKS.md` T1.3.
