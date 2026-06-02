# `intraday/data/` — data adapters + parquet store

Theta intraday pull/stream adapters and the partitioned parquet store.

- **Reuse:** `engine/theta_connector.py` (tier-aware connector),
  `scripts/pull_theta_option_tape.py` (option trades + NBBO + side-inference),
  `data/feature_store.py` (parquet conventions).
- **Scope (Phase 1):** SPX (1s), SPY/QQQ (1m + tick), option chain snapshots,
  ATM±10 option tape incl. 0DTE. See `../DESIGN.md` §2.
- **Rule:** every row PIT-stamped (no look-ahead). Halt on feed gaps; never
  emit a signal on stale data.
- **Tasks:** `../TASKS.md` P0, T0.1, T0.2. Record the tier-probe result here.
