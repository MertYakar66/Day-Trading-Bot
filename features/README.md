# `intraday/features/` — intraday feature builders

Recompute the upstream quant math on intraday data.

- **GEX / gamma-flip / walls** — wrap `engine/dealer_positioning.py`.
- **Order-flow imbalance (OFI)** — from the tape's `side_inferred`.
- **Realized vol (intraday)** — `engine/realized_vol.py` (Yang-Zhang).
- **VRP** — ATM IV (chain) − intraday RV.
- **Skew / term-structure** — `engine/skew_dynamics.py`,
  `engine/volatility_surface.py`.
- **VWAP + bands, opening range.**

See `../DESIGN.md` §4. No look-ahead. Tasks: `../TASKS.md` T0.3.
