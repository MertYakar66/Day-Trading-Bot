# `intraday/risk/` — sizing, stops, kill-switch

- **Sizing:** fractional Kelly (`engine/risk_manager.py`), capped per-trade and
  per-underlying.
- **Per-trade stop:** intraday-sigma or structural-level, set at entry.
- **Daily loss limit:** hard kill-switch (default 3% of paper NAV).
- **Concurrency cap** + **PDT awareness** (log trade count / equity assumptions).

See `../DESIGN.md` §7. Paper NAV is an open question — do not hardcode a
live-money value (`../DESIGN.md` §11).
