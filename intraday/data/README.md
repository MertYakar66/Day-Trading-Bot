# `intraday/data/` — data adapters + parquet store

Theta intraday pull/stream adapters and the partitioned parquet store.

- **Implemented:** `provider.py` (`DataProvider` ABC), `synthetic.py`
  (deterministic `SyntheticDataProvider` — the Phase-0 workhorse), `theta_adapter.py`
  (real path over `engine/theta_connector.py`; **never connected this session**),
  `store.py` (`ParquetStore`, DESIGN §2.3 `ticker=/date=` layout), `quality.py`
  (liquidity/staleness predicates).
- **Scope (Phase 1):** SPX (1s), SPY/QQQ (1m + tick), option chain snapshots,
  ATM±10 option tape incl. 0DTE. See `../DESIGN.md` §2.
- **Rule:** every row PIT-stamped (no look-ahead). Halt on feed gaps; never
  emit a signal on stale data.
- **Tasks:** `../TASKS.md` P0, T0.1, T0.2.

## Tier-probe result (P0) — recorded 2026-06-01

**Theta subscription = FREE tier.** Only `/v3/stock/history/eod` is unlocked;
SPX/VIX index, real-time/intraday stock, the stock trade tape, and ALL option
data are gated behind STANDARD/VALUE. **Real intraday data is therefore
unavailable**, and the engine is built/validated entirely against
`SyntheticDataProvider` (labelled synthetic everywhere). Full detail and the
re-activation steps are in [`../docs/THETA_TIER_PROBE.md`](../docs/THETA_TIER_PROBE.md).
Do not re-probe or connect to Theta while the operator is using it concurrently.
