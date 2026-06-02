# `intraday/authority/` — the decision gate

The intraday analogue of the wheel engine's `EVEngine.evaluate`. Mirrors
`CLAUDE.md` §2: **one authority + downgrade-only reviewers.**

- **Expectancy gate (authority):** a signal is tradeable only if
  `E[net PnL] = p·win − (1−p)·loss − costs > threshold`, costs from
  `engine/transaction_costs.py`. Negative / non-finite → blocked.
- **Downgrade-only reviewers** (can demote, never upgrade):
  event lockout (`engine/event_gate.py`), regime filter (`engine/regime_*`),
  liquidity gate, daily kill-switch (`engine/risk_manager.py`).

Nothing reaches the paper ledger without passing the gate. See `../DESIGN.md`
§6. Tasks: `../TASKS.md` T0.6, T0.7.
