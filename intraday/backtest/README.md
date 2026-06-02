# `intraday/backtest/` — event-driven intraday simulator

- **Replay:** stored bars + tape in timestamp order, **no look-ahead**.
- **Fill model:** conservative — next-bar / NBBO with modelled slippage, never
  the touch you "saw." Costs from `engine/transaction_costs.py`.
- **Base to extend:** `backtests/simulator.py` (upstream).
- **Metrics:** `engine/performance_metrics.py` — net Sharpe/Sortino, max DD,
  hit-rate, payoff, expectancy/trade, turnover, cost drag.

The backtest and the paper ledger must emit the same record shape. See
`../DESIGN.md` §8. Tasks: `../TASKS.md` T0.4, T0.5.
