# SWE API Reference (read-only dependency at vendor/swe)

> Auto-generated 2026-06-01 by a read-only mapping pass over vendor/swe source.
> Purpose: let the intraday engine CALL the reused SWE quant modules correctly.
> The SWE submodule is READ-ONLY — never edit it; propose changes upstream.

## engine/transaction_costs.py

**Module path:** `vendor/swe/engine/transaction_costs.py`
**One-line purpose:** The cost authority for the wheel strategy — centralizes commission, slippage (spread-based + Almgren-Chriss sqrt market impact), fees, and Reg-T margin so the simulator and label generator apply costs identically.

### Import-time behavior / dependencies

- **The module file itself is dependency-free at runtime.** Its only imports are `logging` and `from typing import Literal` (top of file), plus a function-local `import math as _math` inside `calculate_slippage`. **No numpy / scipy / pandas / statsmodels / arch / sklearn / requests.** You can vendor or import this single file in isolation with zero third-party deps.
- **GOTCHA — package import path triggers heavy imports.** If you do `from engine.transaction_costs import calculate_total_entry_cost`, Python first executes `engine/__init__.py`, which eagerly imports the whole engine surface (ev_engine, option_pricer, monte_carlo, volatility_surface, etc.). That pulls in **numpy, pandas, scipy** (and across the package, statsmodels/arch/sklearn/requests are used by sibling modules). 41 of the engine's modules import at least one of those. To avoid this, import the file directly without going through the package `__init__` (e.g. load it as a standalone module / copy it), or be prepared to have numpy+pandas+scipy installed.
- All public symbols are plain module-level functions. **There are no classes or dataclasses in this module.**

### Module-level constants (public, overridable by editing, not by argument)

- `DEFAULT_COMMISSION_PER_CONTRACT = 0.65` — dollars per option contract.
- `DEFAULT_ASSIGNMENT_FEE = 5.00` — dollars per assignment.
- `DEFAULT_SLIPPAGE_PCT = 0.15` — base slippage as a fraction of the bid-ask spread (15% of spread). NOTE: these are module globals; the functions do **not** take a config object and do **not** accept overrides as parameters (except `impact_coefficient`, `fallback_pct`). To change commission/assignment/base-slippage you must monkeypatch the module global.

---

### Universal units convention (read first)

- **All prices/premiums passed in are per-SHARE, in dollars** (e.g. a $1.50 option premium = `1.50`, NOT 150 and NOT cents).
- **Contract multiplier is hard-coded to 100 shares.** The aggregate functions multiply per-share values by `* 100` internally. `calculate_assignment_costs` takes a `shares` arg defaulting to 100 but the option-cost functions do not — they assume exactly one contract = 100 shares.
- **Spread is in dollars** (ask − bid), per share.
- **Slippage returned by `calculate_slippage` is per-SHARE dollars; slippage inside the `*_cost` dict returns is per-CONTRACT dollars** (already ×100). This is the single biggest gotcha.
- Margin / strike / underlying prices are dollars per share; `calculate_reg_t_margin_short_put` returns dollars per contract.

---

### `calculate_commission(trade_type: str = "option", num_contracts: int = 1) -> float`

- Returns total commission in **dollars**.
- Lookup table: `"option"` → `0.65/contract`, `"stock"` → `0.0`. Any unknown `trade_type` falls back to `0.65/contract` (the option rate), NOT zero.
- For stock, `num_contracts` means 100-share lots (but rate is 0 anyway).
- Per-contract, not per-share.

### `calculate_actual_spread(bid: float | None, ask: float | None, mid_price: float | None = None, fallback_pct: float = 0.10) -> float`

- Returns the bid-ask **spread in dollars** (per share).
- Logic: if `bid` and `ask` both valid and `ask >= bid >= 0` → returns `ask - bid`. Else if `mid_price > 0` → returns `mid_price * fallback_pct` (default 10% of mid). Else uses whichever of ask/bid is >0 as basis × `fallback_pct`; returns `0` if nothing usable.

### `calculate_slippage(...) -> float`  ← the core impact model

```python
calculate_slippage(
    mid_price: float,
    bid_ask_spread: float,
    trade_direction: Literal["buy", "sell"],
    open_interest: int | None = None,
    volume: int | None = None,          # accepted but UNUSED (API-compat only)
    num_contracts: int = 1,
    adv_contracts: int | None = None,
    use_sqrt_impact: bool = True,
    impact_coefficient: float = 0.10,
) -> float
```

- **Returns per-SHARE slippage in dollars, always positive (`abs`).** `trade_direction` does NOT change the sign or magnitude of the return — it is documented intent only (caller applies sign: sell → mid−slip, buy → mid+slip). Both `"buy"` and `"sell"` yield the same positive number.
- Formula: `slippage = spread_slippage + size_slippage`.
  - `spread_slippage = base_factor * bid_ask_spread`, where `base_factor` starts at `DEFAULT_SLIPPAGE_PCT = 0.15` and is scaled up by liquidity/spread penalties, then **capped at 0.50** (max 50% of spread width).
    - Open-interest penalty (multiplicative): `OI < 50` ×2.5; `OI < 100` ×2.0; `OI < 500` ×1.5; else ×1. Applied only if `open_interest is not None`.
    - Wide-spread penalty (multiplicative): `spread/mid > 0.50` ×2.0; `> 0.30` ×1.5. Applied only if `mid_price > 0`.
  - `size_slippage = impact_coefficient * mid_price * sqrt(num_contracts / adv_contracts)` (Almgren-Chriss / Kyle-lambda square-root impact). **Only computed when `use_sqrt_impact=True` AND `adv_contracts is not None and > 0` AND `num_contracts > 0`.** Otherwise `size_slippage = 0.0` (clean fallback to spread-only, backward-compatible).
- **MUST-supply for the sqrt term:** `adv_contracts` (average daily volume in **contracts**) and `num_contracts`. Without `adv_contracts`, order-size impact is silently zero — the docstring warns spread-only understates illiquid large lots by ~70%.
- `impact_coefficient` default `0.10` is conservative; calibrate per broker/venue. Units: dimensionless multiplier on `mid_price`.
- **GOTCHA:** the two aggregate functions below (`calculate_total_entry_cost` / `calculate_total_exit_cost`) call `calculate_slippage` **without** passing `num_contracts`/`adv_contracts`, so they ALWAYS use the spread-only path (no sqrt impact). To get sqrt market impact you must call `calculate_slippage` directly.

### `calculate_assignment_fee() -> float`

- Returns `DEFAULT_ASSIGNMENT_FEE = 5.00` dollars. No args.

### `calculate_reg_t_margin_short_put(strike: float, underlying_price: float, premium: float) -> float`

- Returns Reg-T margin requirement **per contract in dollars**.
- All three inputs are **per-share dollars**. `premium` is premium collected per share.
- `margin = max( 0.20*underlying*100 - max(0, strike-underlying)*100 + premium*100 , 0.10*strike*100 + premium*100 , 100.0 )`. The `*100` is the contract multiplier; `$100/contract` is the floor.

---

### `calculate_total_entry_cost(...) -> dict`  ← opening a SHORT option (selling)

```python
calculate_total_entry_cost(
    premium_per_share: float,
    bid_ask_spread: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    trade_type: str = "option",
    open_interest: int | None = None,
    volume: int | None = None,
) -> dict
```

- If `bid_ask_spread is None`, it is computed via `calculate_actual_spread(bid, ask, premium_per_share)`.
- Uses `trade_direction="sell"`; spread-only slippage (no sqrt impact).
- **Return dict (all dollar values; per-CONTRACT except `effective_fill_price`):**
  - `"commission"` — per contract (0.65).
  - `"slippage"` — per CONTRACT (= per-share slippage × 100).
  - `"total_cost"` — `commission + slippage_per_contract`. **This is the total trade cost for entry.**
  - `"gross_premium"` — `premium_per_share * 100`.
  - `"net_premium_collected"` — `gross_premium - slippage_per_contract - commission`. Net of all costs; what you actually receive.
  - `"effective_fill_price"` — per SHARE: `premium_per_share - slippage_per_share` (you sell below mid).

### `calculate_total_exit_cost(...) -> dict`  ← closing a SHORT option (buying back)

```python
calculate_total_exit_cost(
    buyback_price_per_share: float,
    bid_ask_spread: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    trade_type: str = "option",
    open_interest: int | None = None,
    volume: int | None = None,
) -> dict
```

- Uses `trade_direction="buy"`; spread-only slippage.
- **Return dict:**
  - `"commission"` — per contract.
  - `"slippage"` — per CONTRACT (×100).
  - `"total_cost"` — `commission + slippage_per_contract`. **Total trade cost for exit.**
  - `"gross_buyback_cost"` — `buyback_price_per_share * 100`.
  - `"total_buyback_cost"` — `gross_cost + slippage_per_contract + commission` (all-in cash to close).
  - `"effective_fill_price"` — per SHARE: `buyback_price_per_share + slippage_per_share` (you buy above mid).

### `calculate_assignment_costs(strike_price: float, shares: int = 100) -> dict`

- `strike_price` per share, dollars. `shares` default 100 (one contract).
- **Return dict:** `"assignment_fee"` (5.00), `"stock_cost"` (`strike*shares`), `"total_cash_required"` (`stock_cost + assignment_fee`).

### `estimate_round_trip_cost(...) -> dict`  ← pre-trade net-of-cost expectancy helper

```python
estimate_round_trip_cost(
    entry_premium: float,
    expected_exit_premium: float,
    entry_spread: float | None = None,
    exit_spread: float | None = None,
    open_interest: int | None = None,
) -> dict
```

- All premiums/spreads per share, dollars. Defaults: `entry_spread = entry_premium * 0.10`; `exit_spread` defaults to `entry_spread`.
- Internally calls `calculate_total_entry_cost` (sell) and `calculate_total_exit_cost` (buy) — so again spread-only slippage, no sqrt impact, single contract.
- **Return dict:**
  - `"entry_costs"` — entry `total_cost` (per contract, $).
  - `"exit_costs"` — exit `total_cost` (per contract, $).
  - `"total_costs"` — sum of both (per contract, $). **This is the round-trip cost to subtract from gross expectancy.**
  - `"cost_as_pct_of_premium"` — `total_costs / (entry_premium * 100)`, a decimal fraction (e.g. 0.085 = 8.5%), `0` if `entry_premium <= 0`.
  - `"breakeven_decay_needed"` — `total_costs / 100`, per-SHARE premium decay required to break even on costs.

---

### Sign conventions & gotchas (summary)

- **Slippage sign:** `calculate_slippage` always returns a positive per-share number regardless of `trade_direction`. Direction only documents how the caller should apply it (sell = mid − slip, buy = mid + slip). The aggregate functions already bake the correct direction into `effective_fill_price` and `net_premium_collected`.
- **Per-share vs per-contract:** raw `calculate_slippage`/`calculate_actual_spread` are per share; the `*_cost` dicts return slippage/commission/premium **per contract (×100)** but their `effective_fill_price` keys are **per share**. Do not double-multiply.
- **Square-root market impact is OFF by default in every aggregate path.** Only `calculate_slippage` with `adv_contracts` set turns it on. The entry/exit/round-trip helpers never pass `num_contracts`/`adv_contracts`, so for serious sizing you must add the size impact yourself by calling `calculate_slippage` directly.
- **Contract multiplier = 100, hard-coded** in the option-cost functions (only `calculate_assignment_costs` exposes `shares`).
- **Commission for unknown `trade_type` falls back to the option rate (0.65), not 0.**
- `volume` is accepted everywhere but **never used** — only `open_interest` drives liquidity adjustment.
- No risk-free rate, no IV, no annualization, no calibrated chain required by this module — it is purely arithmetic on prices/spreads/sizes you supply.

---

### Minimal copy-pasteable usage

```python
# Import the file directly to avoid engine/__init__.py heavy imports (numpy/pandas/scipy):
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "transaction_costs",
    pathlib.Path("vendor/swe/engine/transaction_costs.py"),
)
tc = importlib.util.module_from_spec(spec); spec.loader.exec_module(tc)
# (or, if numpy/pandas/scipy are installed:  from engine.transaction_costs import ... )

# 1) Net-of-cost entry: sell a 1.50 put, bid 1.45 / ask 1.55, OI 800
entry = tc.calculate_total_entry_cost(
    premium_per_share=1.50, bid=1.45, ask=1.55, open_interest=800,
)
# entry["net_premium_collected"]  -> dollars received after slippage+commission (per contract)
# entry["total_cost"]             -> dollars of cost (per contract)

# 2) Round-trip pre-trade expectancy (subtract total_costs from gross EV):
rt = tc.estimate_round_trip_cost(
    entry_premium=1.50, expected_exit_premium=0.50,
    entry_spread=0.10, open_interest=800,
)
gross_ev_per_contract = (1.50 - 0.50) * 100          # naive decay capture
net_ev_per_contract   = gross_ev_per_contract - rt["total_costs"]

# 3) Size-aware slippage (the ONLY way to get sqrt market impact):
slip_per_share = tc.calculate_slippage(
    mid_price=1.50, bid_ask_spread=0.10, trade_direction="sell",
    open_interest=800, num_contracts=10, adv_contracts=100,
    use_sqrt_impact=True, impact_coefficient=0.10,
)
slip_per_contract = slip_per_share * 100             # apply your own ×100

# 4) Margin for the short put (per contract, dollars):
margin = tc.calculate_reg_t_margin_short_put(strike=50.0, underlying_price=52.0, premium=1.50)

# 5) Assignment cash + fee for 1 contract:
assign = tc.calculate_assignment_costs(strike_price=50.0, shares=100)
# assign["total_cash_required"], assign["assignment_fee"]
```

---

## engine/dealer_positioning.py

**Module path:** `vendor/swe/engine/dealer_positioning.py` (import as `engine.dealer_positioning`).
**One-line purpose:** Convert a single-expiry option chain + spot into a `MarketStructure` (dollar GEX/DEX, put/call gamma walls, gamma-flip level, pinning zones, regime label + confidence), and map the regime to a clamped EV multiplier in `[0.70, 1.05]`.

### Import-time cost / third-party deps
At module-import time the file does `import numpy as np`, `import pandas as pd`, and `from .option_pricer import black_scholes_all_greeks`. `option_pricer` itself imports `numpy`, `pandas`, and `scipy` (`from scipy.optimize import brentq`, `from scipy.stats import norm`). So importing this module requires **numpy, pandas, and scipy**. No statsmodels/arch/sklearn/requests; no network.

Importing via the package `import engine` (i.e. `engine/__init__.py`) is **heavy**: `engine/__init__.py` eagerly imports ~15 sibling modules (ev_engine, monte_carlo, regime_detector, risk_manager, volatility_surface, wheel_runner, etc.) and re-exports `MarketStructure`. To avoid that, import the submodule directly: `from engine.dealer_positioning import ...`. That direct import only pulls numpy/pandas/scipy + option_pricer (no broker/runner machinery).

### Public enum: `DealerAssumption(StrEnum)`
Two members (string values):
- `DealerAssumption.LONG_CALLS_SHORT_PUTS = "long_calls_short_puts"` (default; SpotGamma convention)
- `DealerAssumption.SHORT_BOTH = "short_both"` (ablation)

Sign mapping in `_signs()`: `LONG_CALLS_SHORT_PUTS → (call_sign=+1, put_sign=-1)`; `SHORT_BOTH → (call_sign=-1, put_sign=-1)`.

### Public dataclass: `PerStrikeExposure`
Per-strike diagnostic record. All fields are **required** (no defaults):
```
strike: float
call_oi: int
put_oi: int
call_gamma: float   # per-share BSM gamma (1/$), NOT dollarized
put_gamma: float
call_delta: float   # per-share BSM delta (call >=0, put <=0)
put_delta: float
call_gex: float     # signed dollars per 1% move (call_sign applied)
put_gex: float      # signed dollars per 1% move (put_sign applied)
net_gex: float      # = call_gex + put_gex (dollars per 1% move)
net_dex: float      # dollars (delta exposure)
net_vanna: float    # aggregate, observability only
net_charm: float    # aggregate, observability only
```

### Public dataclass: `GammaWall`
```
strike: float
distance_pct: float           # SIGNED, decimal: (strike - spot)/spot. +above spot, -below.
net_gex: float                # dollars per 1% move (call walls >0, put walls <0)
side: Literal["call", "put"]
```

### Public dataclass: `MarketStructure`
Required (positional/keyword, no defaults):
```
ticker: str
as_of: datetime               # naive datetime (analyzer strips tzinfo)
spot: float
expiry: date
assumption: DealerAssumption
```
Optional (with defaults):
```
gex_total: float = 0.0        # SIGNED dollars per 1% underlying move
dex_total: float = 0.0        # SIGNED dollars
vanna_total: float = 0.0      # observability only — NOT used by multiplier
charm_total: float = 0.0      # observability only — NOT used by multiplier
per_strike: list[PerStrikeExposure] = []
call_walls: list[GammaWall] = []          # top-N positive-net_gex strikes
put_walls: list[GammaWall] = []           # top-N negative-net_gex strikes
nearest_call_wall: GammaWall | None = None  # nearest >= spot within near_wall_pct
nearest_put_wall: GammaWall | None = None   # nearest <= spot within near_wall_pct
flip_level: float | None = None             # spot price where total GEX crosses 0
flip_distance_pct: float | None = None      # SIGNED decimal: (flip_level - spot)/spot
pinning_zones: list[float] = []             # strikes near spot with high OI*|gamma|
regime: Literal["long_gamma_dampening","short_gamma_amplifying","near_flip","neutral"] = "neutral"
confidence: float = 0.0       # [0,1]
n_strikes: int = 0
n_calls: int = 0
n_puts: int = 0
notes: str = ""               # degradation reason, e.g. "empty_chain_or_zero_spot", "missing_columns:...", "no_valid_rows"
```
Method: `MarketStructure.to_dict() -> dict` — JSON-safe; serializes datetimes/dates via `.isoformat()`, walls via `_wall_to_dict`, and a trimmed per-strike list (`strike, call_oi, put_oi, net_gex, net_dex` only). A consumer reading exposures programmatically should use the dataclass fields, not `to_dict()` (which drops greeks/gex split).

### Public class: `DealerPositioningAnalyzer`
Pure-function analyzer, no I/O / no caching / no hidden state.

Constructor:
```python
DealerPositioningAnalyzer(
    assumption: DealerAssumption = DealerAssumption.LONG_CALLS_SHORT_PUTS,
    risk_free_rate: float = 0.05,     # DECIMAL (0.05 = 5%), used to recompute Greeks
    flip_neighborhood_pct: float = 0.01,  # decimal; |flip_distance_pct| below this => "near_flip"
    near_wall_pct: float = 0.05,      # decimal; max |distance_pct| for a wall to be "nearest"
    top_walls: int = 3,               # walls returned per side
    pin_window_pct: float = 0.05,     # decimal; +/- window around spot for pinning zones
) -> None
```

Primary method:
```python
analyze(
    chain: pd.DataFrame,
    spot: float,                 # underlying price in DOLLARS (not cents)
    expiry: date,                # datetime.date of this single expiry
    ticker: str = "",
    dividend_yield: float = 0.0, # DECIMAL continuous q (0.02 = 2%)
    as_of: datetime | None = None,  # naive/PIT timestamp; defaults to utcnow (naive)
) -> MarketStructure
```

**Required `chain` columns** (case-insensitive — columns are lowercased internally):
- `strike` (float, dollars)
- `option_type` ('C'/'P' or 'call'/'put' — normalized by uppercasing and taking the first char, so "Call"/"PUT" also work)
- `open_interest` (int, number of contracts)
- `implied_vol` (float, **DECIMAL** annualized vol — e.g. 0.25 for 25%; rows with iv<=0 or iv>=5.0 are dropped)

**Optional `chain` columns** (used if finite, else recomputed via BSM):
- `delta`, `gamma` — per-share. Reused when present and finite (an exactly-0.0 stored value is honored). Vanna/charm are **always** recomputed from BSM regardless.
- `ticker` — used to fill `ms.ticker` only if the `ticker` arg is empty.
- `expiration` — not used for math; `expiry` argument drives time-to-expiry.

The chain is a **single expiry**. Calls and puts share rows distinguished by `option_type`; multiple rows per (strike,type) are aggregated (OI summed, IV averaged).

**Time to expiry** is computed PIT-safe: `T = max((expiry - as_of.date()).days, 1) / 365.0` (365-day year, floored at 1 day). Caller must pass a sensible `as_of` for backtests or T collapses.

Graceful degradation: empty/None chain, `spot <= 0`, missing required columns, all-invalid rows, or no C/P rows → returns a `MarketStructure` with `regime="neutral"`, `confidence=0.0`, and a `notes` string; it never raises.

### GEX math and units (exact)
Per strike, per side (from `_per_strike_exposures`), with `call_sign/put_sign` from the assumption:
```
gex   = sign * gamma * OI * 100 * spot * spot * 0.01      # dollars per 1% move
net_dex = sign * delta * OI * 100 * spot                  # dollars
net_vanna = sign * vanna * OI * 100
net_charm = sign * charm * OI * 100
```
- `100` is the **contract multiplier** (shares/contract) — hardcoded, not configurable.
- `gamma` is **per-share BSM gamma** (units 1/$). The formula linearizes to dollar-delta-change per 1% move (`gamma * spot * (0.01*spot) * shares`), NOT the 0.5·Γ·dS² P&L form — this is the SpotGamma convention noted in the source comment.
- **Units of `gex_total` / `net_gex`: dollars of dealer hedging flow per 1% underlying move.** `spot` must be in dollars; `implied_vol` decimal; `risk_free_rate`/`dividend_yield` decimal.

### Sign convention / regime (the load-bearing part)
- **Positive `gex_total` ⇒ dealers LONG gamma ⇒ they sell rallies / buy dips ⇒ realized vol DAMPENED ⇒ `regime="long_gamma_dampening"`.** Favorable for premium sellers; multiplier ≥ 1.
- **Negative `gex_total` ⇒ dealers SHORT gamma ⇒ they chase moves ⇒ vol AMPLIFIED ⇒ `regime="short_gamma_amplifying"`.** Multiplier < 1.
- `gex_total == 0` with no flip info ⇒ `"neutral"`.
- **`near_flip` dominates:** if `flip_distance_pct` is not None and `abs(flip_distance_pct) < flip_neighborhood_pct` (default 1%), regime is `"near_flip"` regardless of the GEX sign (intraday flip risk).

**Walls** (`_find_walls`): `call_walls` = strikes with the largest **positive** `net_gex` (long-gamma concentration → pinning/resistance); `put_walls` = largest **negative** `net_gex` (short-gamma/gamma-cliff → support). Each side returns up to `top_walls`. `nearest_call_wall` is the closest call wall **at/above** spot within `near_wall_pct`; `nearest_put_wall` is closest put wall **at/below** spot within `near_wall_pct` (else None). `GammaWall.distance_pct` is signed decimal `(strike-spot)/spot`.

**Gamma-flip** (`_solve_flip_level`): scans total GEX over 30 points across `[0.7·spot, 1.3·spot]` (re-pricing gamma at each candidate spot), finds sign changes of total GEX, picks the bracket whose midpoint is closest to current spot, and **linearly interpolates** the zero crossing. Returns `None` when no sign change exists in the band (regime unambiguously long or short gamma everywhere scanned). `flip_distance_pct = (flip_level - spot)/spot` (signed decimal), else None. Note this re-computes Greeks per candidate spot (an `iterrows` loop) — it is the most expensive part.

**Pinning zones** (`_detect_pinning_zones`): strikes within `±pin_window_pct·spot` whose score `(call_oi+put_oi)·(|call_gamma|+|put_gamma|)` is ≥ the 75th percentile (and >0). Returns a `list[float]` of strikes.

**Confidence** (`_regime_confidence`): `neutral`→0.0; `near_flip`→fixed 0.50; otherwise `min(1.0, (sum |net_gex| of strikes whose sign matches gex_total sign) / (sum |net_gex| all strikes))` — i.e. fraction of GEX aligned with the dominant sign, in `[0,1]`.

### Public function: `dealer_regime_multiplier`
```python
dealer_regime_multiplier(ms: MarketStructure | None) -> float
```
Returns a scalar in **[0.70, 1.05]**. Confidence (clamped to [0,1]) scales the distance from 1.0:
- `ms is None` → `1.0`
- `regime == "long_gamma_dampening"` → `1.0 + 0.05*conf` (max **1.05**)
- `regime == "short_gamma_amplifying"` → `1.0 - 0.30*conf` (min **0.70**)
- `regime == "near_flip"` → flat **0.85** (ignores confidence)
- `regime == "neutral"` (or anything else) → `1.0`

**Gotchas:** Asymmetric by design (small upside cap 1.05, large downside cut 0.70) — do not "fix" the asymmetry. Per the repo invariant (CLAUDE.md §2), this multiplier scales `ev_dollars` only, never `ev_raw`; it is non-negative so it can never flip the sign of EV (cannot rescue a negative-EV trade). Clamp `[0.70, 1.05]` must not be altered.

### Caller-supplied non-obvious requirements
- IV must be **decimal annualized** (0.25, not 25). Spot/strike in **dollars**. `risk_free_rate` and `dividend_yield` **decimal**.
- Contract multiplier is fixed at 100 internally — do **not** pre-multiply OI by 100.
- Pass a correct `as_of` (naive datetime) for historical/backtest chains or `T` and all Greeks are wrong.
- One `analyze` call = **one expiry**. Aggregate across expiries yourself if needed.
- No calibrated surface needed; Greeks are reconstructed from `implied_vol` per row via BSM if `delta`/`gamma` are absent.
- The instance is stateless/reusable across tickers and expiries.

### Minimal copy-pasteable example
```python
from datetime import date, datetime
import pandas as pd
from engine.dealer_positioning import (
    DealerPositioningAnalyzer, DealerAssumption, dealer_regime_multiplier,
)

chain = pd.DataFrame([
    {"strike": 95.0,  "option_type": "P", "open_interest": 1200, "implied_vol": 0.30},
    {"strike": 100.0, "option_type": "C", "open_interest": 3000, "implied_vol": 0.25},
    {"strike": 100.0, "option_type": "P", "open_interest": 1500, "implied_vol": 0.27},
    {"strike": 105.0, "option_type": "C", "open_interest": 2500, "implied_vol": 0.24},
])

analyzer = DealerPositioningAnalyzer(
    assumption=DealerAssumption.LONG_CALLS_SHORT_PUTS,  # default
    risk_free_rate=0.05,                                # decimal
)

ms = analyzer.analyze(
    chain=chain,
    spot=100.0,                       # dollars
    expiry=date(2026, 7, 17),
    ticker="AAPL",
    dividend_yield=0.0,               # decimal
    as_of=datetime(2026, 6, 1),       # naive; PIT-safe T
)

print(ms.regime, ms.gex_total, ms.flip_level, ms.flip_distance_pct)
print(ms.nearest_call_wall, ms.nearest_put_wall)

mult = dealer_regime_multiplier(ms)   # in [0.70, 1.05]
ev_dollars = ev_raw * mult            # multiplier scales ev_dollars only
```

---

## engine/realized_vol.py

**Module path:** `vendor/swe/engine/realized_vol.py` (importable as `engine.realized_vol`).
**One-line purpose:** OHLC realized-volatility estimators (close-to-close, Parkinson, Garman-Klass, Rogers-Satchell, Yang-Zhang) plus a bundle helper and an IV-minus-RV "vol risk premium" bundle.

### Import-time behavior / dependencies
- `import engine.realized_vol` is **light**: top-level imports are only `numpy` and `pandas` (plus `from __future__ import annotations`). No scipy/statsmodels/arch/sklearn/requests.
- IMPORTANT GOTCHA: `engine.realized_vol` is **NOT** re-exported by `engine/__init__.py` and is not in its `__all__`. But `engine/__init__.py` itself runs very heavy eager imports (ev_engine, wheel_runner, monte_carlo, volatility_surface, risk_manager, etc., pulling scipy/statsmodels/etc.). So `from engine import realized_vol` or `import engine.realized_vol` will trigger the whole package `__init__` and its transitive deps. To avoid that, import the file directly as a standalone module (e.g. add `vendor/swe/engine` to path and `import realized_vol`), or be prepared for the full SWE dependency set to load.
- Module-level constant: `_TRADING_DAYS = 252` (the annualization factor; hard-coded, not a parameter).

### Conventions (apply to ALL estimators)
- Input: a `pandas.DataFrame`. Required columns depend on estimator (see each below). Column names are lowercase: `open`, `high`, `low`, `close`.
- The functions operate on the **last `window` bars** (via `df.tail(...)`), so the DataFrame may be longer than `window`; only the tail is used. Order matters — rows must be in chronological order (oldest first, newest last).
- **Output is ANNUALIZED volatility as a decimal** (e.g. `0.2617` = 26.17%), using `sqrt(252)` / `*252` scaling. Bars are assumed to be **daily**.
- Bad bars (non-positive prices) are mapped to NaN internally (`_log`), so a bad bar yields NaN rather than ±inf. Insufficient rows return `float("nan")` (not an exception).
- GK, RS, YZ clamp negative variance to 0 before sqrt (`max(var, 0.0)`); Parkinson and close-to-close do not (but their variance is non-negative by construction).

### Public functions

```python
close_to_close_vol(df: pd.DataFrame, window: int = 20) -> float
```
- Required columns: `close`. Needs `len(df) >= window + 1` (uses `window+1` closes to form `window` log-returns). Returns NaN if `df` is None/empty or too short.
- Math: `std(diff(log(close)), ddof=1) * sqrt(252)`. Sample std (ddof=1). Units: annualized decimal vol.

```python
parkinson_vol(df: pd.DataFrame, window: int = 20) -> float
```
- Required columns: `high`, `low`. Needs `len(df) >= window`.
- Math: `var = (1/(4*ln2)) * mean( ln(H/L)^2 )`; returns `sqrt(var * 252)`. Units: annualized decimal vol.

```python
garman_klass_vol(df: pd.DataFrame, window: int = 20) -> float
```
- Required columns: `open`, `high`, `low`, `close`. Needs `len(df) >= window`. Assumes zero drift.
- Math: `var = mean( 0.5*ln(H/L)^2 - (2*ln2 - 1)*ln(C/O)^2 )`; returns `sqrt(max(var,0) * 252)`. Units: annualized decimal vol.

```python
rogers_satchell_vol(df: pd.DataFrame, window: int = 20) -> float
```
- Required columns: `open`, `high`, `low`, `close`. Needs `len(df) >= window`. Drift-independent (good for trending markets).
- Math: `var = mean( ln(H/C)*ln(H/O) + ln(L/C)*ln(L/O) )`; returns `sqrt(max(var,0) * 252)`. Units: annualized decimal vol.

```python
yang_zhang_vol(df: pd.DataFrame, window: int = 20, k: float | None = None) -> float
```
- Required columns: `open`, `high`, `low`, `close`. Needs `len(df) >= window + 1` (uses one extra prior bar for overnight return `ln(O_t/C_{t-1})`).
- Math: `σ_YZ^2 = var_overnight + k*var_open_to_close + (1-k)*var_RS`, returns `sqrt(max(σ_YZ^2,0) * 252)`.
  - `var_overnight = var(ln(O_t/C_{t-1}), ddof=1)`, `var_open_to_close = var(ln(C_t/O_t), ddof=1)`, `var_RS = mean(RS term)`.
  - `k` default (when `None`): `k = 0.34 / (1.34 + (n+1)/max(n-1,1))` where `n = window` (number of overnight returns). Pass an explicit `k` to override the optimal weight. Units: annualized decimal vol.

```python
realised_vol_bundle(df: pd.DataFrame, window: int = 20) -> dict
```
- Returns dict with keys: `"close_to_close"`, `"parkinson"`, `"garman_klass"`, `"rogers_satchell"`, `"yang_zhang"`, each a float annualized decimal vol (may be NaN). Note the British spelling `realised` in the function name.

```python
vol_risk_premium_bundle(df: pd.DataFrame, iv_atm: float, window: int = 20) -> dict
```
- `iv_atm`: ATM implied vol as an **annualized decimal** (e.g. `0.30`), same units as RV output. Caller must supply this.
- Returns a merged dict containing:
  - all 5 raw RV keys (from `realised_vol_bundle`),
  - `vrp_<estimator>` for each (= `iv - rv`; NaN if rv non-finite). **Sign convention: positive VRP = IV richer than realized = premium-selling edge.**
  - `consensus_rv` = mean of the finite values among {garman_klass, rogers_satchell, yang_zhang} (the 3 "robust" estimators), NaN if none finite.
  - `consensus_vrp` = `iv - consensus_rv`.
  - `iv_atm` = the passed-in IV (echoed back).

### Minimal usage example
```python
import pandas as pd
import realized_vol  # if imported as a standalone file; else: from engine import realized_vol

df = pd.DataFrame({
    "open":  [100, 101, 102, 101, 103, 104, 103, 105, 106, 107,
              108, 107, 109, 110, 111, 110, 112, 113, 114, 115, 116],
    "high":  [101, 102, 103, 102, 104, 105, 104, 106, 107, 108,
              109, 108, 110, 111, 112, 111, 113, 114, 115, 116, 117],
    "low":   [ 99, 100, 101, 100, 102, 103, 102, 104, 105, 106,
              107, 106, 108, 109, 110, 109, 111, 112, 113, 114, 115],
    "close": [101, 102, 101, 103, 104, 103, 105, 106, 107, 108,
              107, 109, 110, 111, 110, 112, 113, 114, 115, 116, 116],
})  # chronological, oldest first; 21 rows so YZ (needs window+1=21) works

ann_yz = realized_vol.yang_zhang_vol(df, window=20)   # e.g. 0.18 == 18% annualized
bundle = realized_vol.realised_vol_bundle(df, window=20)
vrp    = realized_vol.vol_risk_premium_bundle(df, iv_atm=0.30, window=20)
# vrp["consensus_vrp"] > 0  => IV richer than realized
```

### Non-obvious must-supply
- Bars must be **daily** (252-day annualization is hard-wired; there is no parameter to change it or to handle intraday bars). For intraday RV you would need to rescale the output yourself (the function does not expose a `trading_days`/`periods` argument).
- `iv_atm` for `vol_risk_premium_bundle` must be a **decimal annualized** number, matching RV units (do not pass percent like `30`).

---

## engine/option_pricer.py

**Module path:** `vendor/swe/engine/option_pricer.py` (importable as `engine.option_pricer`).
**One-line purpose:** Black-Scholes-Merton European pricing + full Greeks (1st/2nd/3rd order), implied-vol solver, Barone-Adesi-Whaley American pricing + numeric American Greeks, and vectorized batch pricing.

### Import-time behavior / dependencies
- `import engine.option_pricer` requires: `numpy`, `pandas`, `scipy` (`scipy.optimize.brentq`, `scipy.stats.norm`), plus stdlib `os`, `typing`. No statsmodels/arch/sklearn/requests at import time.
- Reads env var at import: `_VALIDATE_GREEKS = os.environ.get("VALIDATE_GREEKS","0") == "1"` (default False/off). It can also be toggled at runtime via `option_pricer._VALIDATE_GREEKS = True`. When True, `black_scholes_all_greeks` does a lazy `from .contracts import validate_greeks_semantics` and emits `warnings.warn` on semantic violations — that lazy import pulls in `engine.contracts` (needs `pandas`; it does relative-import `from .contracts`, so it requires being imported as part of the `engine` package). Leave it off to avoid that coupling.
- `engine/__init__.py` eagerly re-exports most of these functions (so importing the package gives you `black_scholes_price`, etc.) but also drags in the whole heavy engine. (Note: `american_option_price`, `american_option_greeks`, `implied_volatility`, `vectorized_bs_*` are present in the module but `implied_volatility`/`american_*` are NOT in the package `__all__`; import them from `engine.option_pricer` directly.)

### Universal unit conventions (CRITICAL)
- `S`, `K`: prices in the **same currency units** (dollars), must be `> 0` (raises `ValueError` otherwise).
- `T`: time to expiration in **years** (e.g. 30 days = `30/365`). `T <= 0` returns intrinsic / deterministic boundary values, not an error.
- `r`: risk-free rate as an **annualized decimal** (e.g. `0.04` = 4%), continuously compounded.
- `q`: continuous dividend yield as an **annualized decimal** (default `0.0`).
- `sigma`: volatility as an **annualized decimal** (e.g. `0.25` = 25%), must be `>= 0`. `sigma <= 0` returns the zero-vol deterministic case.
- `option_type`: literal string `"call"` or `"put"` (validated; anything else raises `ValueError`).
- All Greeks are **per share** (per 1 unit of underlying), NOT per contract. To get per-contract values multiply by the contract multiplier (typically 100) yourself — the module never applies a multiplier.

### Greek units / scaling (audited, non-standard scalings noted)
- **price**: dollars per share.
- **delta**: per $1 move in S. Range `[0,1]` calls (`e^{-qT}·N(d1)`), `[-1,0]` puts (`e^{-qT}·(N(d1)-1)`).
- **gamma**: per $1 move, per share; always `>= 0`; same for calls/puts. NOT scaled.
- **theta**: **per YEAR** (annual time decay), typically negative for long options. Divide by 365 for daily theta. NOT scaled.
- **vega**: **per 1 vol POINT** (per 0.01 change in sigma). The raw BS vega `S·e^{-qT}·N'(d1)·√T` is **divided by 100**. So `vega=0.114` means a +1 vol-point move (25%→26% IV) raises price ~$0.114/share. For a decimal `dsigma`, P&L = `vega * dsigma * 100`; for vol-points `dvolpts`, P&L = `vega * dvolpts`.
- **rho**: **per 1% (100 bps) rate change** (raw BS rho **divided by 100**). `rho=-0.041` ⇒ +100 bps (0.05→0.06) drops a put ~$0.041/share. For decimal `dr`, P&L = `rho * dr * 100`. Positive for calls, negative for puts.
- **vanna** = ∂Delta/∂σ = `-e^{-qT}·N'(d1)·d2/σ` (raw, NOT /100). 
- **charm** = ∂Delta/∂t, **calendar-time convention** (positive charm ⇒ delta increases as time passes; this is `-∂Delta/∂τ`). Per year. NOT scaled.
- **volga (vomma)** = ∂Vega/∂σ = `vega·d1·d2/σ` — note it uses the already-/100-scaled `vega`, so volga inherits the per-vol-point scaling.
- **speed** = ∂Gamma/∂S = `-gamma·(d1/(σ√T)+1)/S`. NOT scaled.
- **color** = ∂Gamma/∂T (gamma bleed), **per year** (divide by 365 for daily). NOT scaled.
- **ultima** = ∂Volga/∂σ = `-vega·(d1·d2·(1-d1·d2)+d1²+d2²)/σ²` — uses /100-scaled vega.

### Public functions — exact signatures

```python
black_scholes_price(S: float, K: float, T: float, r: float, sigma: float,
                    option_type: Literal["call","put"], q: float = 0.0) -> float
```
Returns option price (dollars/share). `T<=0` → intrinsic (`max(0,S-K)` / `max(0,K-S)`); `sigma<=0` → discounted intrinsic forward value.

```python
black_scholes_delta(S, K, T, r, sigma, option_type: Literal["call","put"], q: float = 0.0) -> float
black_scholes_gamma(S, K, T, r, sigma, q: float = 0.0) -> float          # no option_type (same both sides)
black_scholes_theta(S, K, T, r, sigma, option_type: Literal["call","put"], q: float = 0.0) -> float
black_scholes_vega (S, K, T, r, sigma, q: float = 0.0) -> float          # no option_type; returns per-vol-point (/100)
black_scholes_rho  (S, K, T, r, sigma, option_type: Literal["call","put"], q: float = 0.0) -> float  # per 1% (/100)
black_scholes_speed (S, K, T, r, sigma, q: float = 0.0) -> float
black_scholes_color (S, K, T, r, sigma, q: float = 0.0) -> float
black_scholes_ultima(S, K, T, r, sigma, q: float = 0.0) -> float
```
(Note `gamma`, `vega`, `speed`, `color`, `ultima` have NO `option_type` param — same for calls and puts. `vega` and `rho` are pre-scaled as above.)

```python
black_scholes_all_greeks(S, K, T, r, sigma, option_type: Literal["call","put"],
                         q: float = 0.0, include_second_order: bool = True) -> dict
```
Most efficient (computes d1/d2 once). Returns dict always containing `price, delta, gamma, theta, vega, rho`; if `include_second_order=True` (default) also `vanna, charm, volga, speed, color, ultima`. Scalings match the individual functions above (`vega`,`rho` /100; `theta`,`color` per year). In the deterministic `T<=0`/`sigma<=0` branch it returns boundary values (gamma/theta/vega/rho/all higher Greeks = 0, delta from moneyness) and drops `vanna/charm/volga` if `include_second_order=False`.

```python
implied_volatility(market_price: float, S: float, K: float, T: float, r: float,
                   option_type: Literal["call","put"], q: float = 0.0,
                   precision: float = 1e-6, max_iterations: int = 100) -> float | None
```
Newton-Raphson with Brent fallback (`brentq` over `[0.001, 10.0]`). Returns **annualized decimal IV**, or **`None`** if: `market_price<=0`, `T<=0`, price below intrinsic (arbitrage), price above theoretical max (call: forward `S·e^{-qT}`; put: `K·e^{-rT}`), or no convergence. `precision` is the price tolerance.

```python
estimate_option_price_from_iv(underlying_price: float, strike: float, dte: int,
                              iv: float, risk_free_rate: float,
                              option_type: Literal["call","put"],
                              dividend_yield: float = 0.0) -> float
```
Convenience wrapper. NOTE: takes `dte` as **integer days** and converts internally `T = dte / 365.0` (365, not 252; differs from the 252-day RV annualization). `iv`, `risk_free_rate`, `dividend_yield` are annualized decimals. Returns European price per share (no early exercise).

```python
american_option_price(S, K, T, r, sigma, option_type: Literal["call","put"], q: float = 0.0) -> float
```
Barone-Adesi-Whaley (1987) approximation. Returns European price for `option_type="call" and q<=0` or `option_type="put" and r<=0` (early exercise not optimal there); otherwise adds the early-exercise premium.

```python
american_option_greeks(S, K, T, r, sigma, option_type: Literal["call","put"], q: float = 0.0,
                       dS: float = 0.01, dT: float = 1/365, d_sigma: float = 0.01, dr: float = 0.01) -> dict
```
Finite-difference Greeks on the BAW price. Returns `{price, delta, gamma, theta, vega, rho}` only (no 2nd/3rd order). `dS` is a **fraction of S** (bump = `S*dS`); central diff for delta/gamma; forward diff for theta (`(price(T-dT)-price)/dT`, negative = decay). `vega` and `rho` are divided by 100 (per 1% move), consistent with the BS scaling.

### Vectorized batch functions

```python
vectorized_bs_price(S, K, T, r, sigma, is_call, q=0.0) -> np.ndarray
vectorized_bs_delta(S, K, T, r, sigma, is_call, q=0.0) -> np.ndarray
vectorized_bs_all_greeks(S, K, T, r, sigma, is_call, q=0.0) -> pd.DataFrame
```
- `S,K,T,sigma` accept `np.ndarray | pd.Series`; `r,q` accept scalar `float` or array/Series; `is_call` is a **boolean** array/Series (`True`=call, `False`=put) — NOT the `"call"/"put"` string used by the scalar API.
- `vectorized_bs_all_greeks` returns a `pd.DataFrame` with columns `price, delta, gamma, theta, vega, rho` (1st-order only; `vega`/`rho` /100 scaled; `theta` per year). Edge cases (`T<=0` or `sigma<=0`) row-wise → intrinsic/boundary values.

### Sign / edge-case gotchas
- `_validate_inputs` raises `ValueError` for `S<=0`, `K<=0`, `sigma<0`, or bad `option_type` — scalar functions can throw; the vectorized ones do NOT validate (they mask edge cases instead). Wrap scalar calls if feeding unsanitized data.
- `theta` and `color` are **annual** — divide by 365 for per-day. `vega` and `rho` are pre-divided by 100 — do not divide again. `gamma`, `delta`, `vanna`, `charm`, `speed`, `ultima` are raw/unscaled.
- `dte`→`T` uses **365** in `estimate_option_price_from_iv`; if you build `T` yourself elsewhere, be consistent.
- These are per-share. Apply the contract multiplier (100) for per-contract P&L/exposure — the caller must supply it.
- For consumers who only need standard valuation, the `engine.shared_valuation` layer wraps these; but the raw functions here have no config object and need no calibrated chain — just the scalar inputs above.

### Minimal usage example
```python
from engine.option_pricer import (
    black_scholes_price, black_scholes_all_greeks,
    implied_volatility, estimate_option_price_from_iv,
)

S, K, r, sigma, q = 100.0, 95.0, 0.04, 0.25, 0.0
T = 30 / 365.0  # 30 calendar days, in YEARS

px = black_scholes_price(S, K, T, r, sigma, "put", q=q)     # dollars/share
g  = black_scholes_all_greeks(S, K, T, r, sigma, "put", q=q)
#   g["delta"] ~ -0.27 ; g["vega"] per 1 vol-pt ; g["theta"] per YEAR
daily_theta   = g["theta"] / 365.0
pnl_1volpt    = g["vega"]                  # +1 vol point
contract_delta = g["delta"] * 100          # per 100-share contract

iv = implied_volatility(market_price=px, S=S, K=K, T=T, r=r,
                        option_type="put", q=q)             # ~0.25 or None

px2 = estimate_option_price_from_iv(underlying_price=S, strike=K, dte=30,
                                    iv=0.25, risk_free_rate=0.04,
                                    option_type="put")       # dte in days
```

---

## engine/performance_metrics.py

**Module path:** `vendor/swe/engine/performance_metrics.py`
**One-line purpose:** Computes risk-adjusted backtest performance metrics (Sharpe, Sortino, max-drawdown, win-rate, profit-factor, Calmar, Ulcer, monthly returns) from a list of closed-trade dicts plus an equity-curve series.

### Import-time behavior / dependencies
- `import engine.performance_metrics` directly imports **`numpy`** and **`pandas`** at module top. No scipy/statsmodels/arch/sklearn/requests.
- WARNING: importing via the package (`from engine import calculate_performance_report`) runs `engine/__init__.py`, which eagerly imports the ENTIRE engine (ev_engine, monte_carlo, volatility_surface, regime_detector, risk_manager, stress_testing, signals, etc.). That transitively pulls heavy third-party deps used elsewhere in the engine. To avoid that, import the submodule directly: `from engine.performance_metrics import calculate_performance_report, PerformanceReport`. (This module file itself only needs numpy + pandas.)

### Public dataclass: `PerformanceReport`
All fields are positional/required (no defaults — the constructor requires every field). Types and units:

| Field | Type | Units / meaning |
|---|---|---|
| `total_return` | `float` | decimal fraction (0.15 = +15%), NOT percent |
| `annualized_return` | `float` | decimal fraction; annualized via `(1+total_return)**(252/num_days) - 1` where `num_days = len(equity_curve)` (number of equity rows, treated as trading days) |
| `total_pnl` | `float` | dollars; `sum(net_pnl)` over closed trades |
| `volatility` | `float` | annualized stdev of daily returns = `returns.std() * sqrt(252)`; decimal fraction |
| `sharpe_ratio` | `float` | annualized, `risk_free_rate` default 0.04 (decimal/yr); see formula below |
| `sortino_ratio` | `float` | annualized, downside-deviation denominator; can be `float('inf')` |
| `max_drawdown` | `float` | POSITIVE magnitude (abs value) of worst peak-to-trough on `portfolio_value`; decimal fraction |
| `max_drawdown_duration` | `int` | days = count of consecutive equity rows with drawdown < 0 (longest run) |
| `total_trades` | `int` | count of closed trades |
| `winning_trades` | `int` | count where `net_pnl > 0` |
| `losing_trades` | `int` | count where `net_pnl < 0` (strictly; `net_pnl == 0` counts as neither) |
| `win_rate` | `float` | `winning_trades/total_trades`; decimal fraction (0.6 = 60%) |
| `profit_factor` | `float` | gross_profit / gross_loss; >1 profitable; `float('inf')` if no losses but profits exist |
| `avg_win` | `float` | mean `net_pnl` of winners (dollars) |
| `avg_loss` | `float` | mean `net_pnl` of losers (dollars, NEGATIVE) |
| `largest_win` | `float` | `max(net_pnl)` (dollars) |
| `largest_loss` | `float` | `min(net_pnl)` (dollars, NEGATIVE) |
| `avg_pnl_per_trade` | `float` | mean `net_pnl` (dollars) |
| `avg_hold_days` | `float` | mean of `hold_days` column (0.0 if column absent) |
| `avg_win_hold_days` | `float` | mean `hold_days` among winners |
| `avg_loss_hold_days` | `float` | mean `hold_days` among losers |
| `total_transaction_costs` | `float` | `sum(transaction_costs)` column (0.0 if absent); dollars |
| `cost_as_pct_of_pnl` | `float` | `total_costs / gross_pnl`; decimal fraction. `gross_pnl = sum(realized_pnl)` if that column exists, ELSE `total_pnl + total_costs`. Convention: `net_pnl = realized_pnl - transaction_costs` |
| `calmar_ratio` | `float` | `annualized_return / max_drawdown` (0.0 if max_dd == 0) |
| `ulcer_index` | `float` | quadratic mean of drawdowns expressed in PERCENT POINTS (drawdown × 100), lower is better |

Methods:
- `to_dict(self) -> dict[str, Any]` — flat dict of all 25 fields.
- `to_dataframe(self) -> pd.DataFrame` — single-row DataFrame.

### Public functions (consumer-facing)

`calculate_performance_report(closed_trades: list[dict], equity_curve: list[dict], initial_capital: float, risk_free_rate: float = 0.04) -> PerformanceReport`
This is the main entry point.
- `closed_trades`: list of dicts. REQUIRED key per trade: **`net_pnl`** (float dollars). OPTIONAL keys consumed: `hold_days`, `transaction_costs`, `realized_pnl`. Missing optional keys silently yield 0.0 for the dependent metrics.
- `equity_curve`: list of dicts. REQUIRED key: **`portfolio_value`** (float dollars) per row. Each row is treated as one trading day; `num_days = len(equity_curve)` drives annualization. (`date` is NOT required here — only `generate_monthly_returns` needs `date`.)
- `initial_capital`: float dollars; denominator for `total_return`. NOTE: if `equity_curve` is empty, `final_value` falls back to `initial_capital` (so total_return=0).
- `risk_free_rate`: ANNUAL rate as a DECIMAL (0.04 = 4%/yr), default 0.04.
- GOTCHA: If `closed_trades` is empty, returns an all-zero `PerformanceReport` immediately (even if equity_curve is populated). The report is only computed when trades exist.

Other public functions:
- `calculate_returns(equity_curve: pd.DataFrame, initial_capital: float) -> pd.Series` — simple period returns `np.diff(values)/values[:-1]` from the `portfolio_value` column. `initial_capital` is accepted but UNUSED inside. Returns empty Series if df empty or column missing.
- `calculate_max_drawdown(equity_curve: pd.DataFrame) -> tuple` — returns `(abs(max_dd): float, duration: int)`. max_dd returned as POSITIVE magnitude.
- `calculate_ulcer_index(equity_curve: pd.DataFrame) -> float` — drawdowns scaled ×100 (percent points).
- `calculate_sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.04, periods_per_year: int = 252) -> float` — formula: `(returns - rf/periods).mean() / returns.std() * sqrt(periods)`. Note: denominator uses raw `returns.std()` (total stdev), numerator uses excess returns. `returns` must be PERIOD (daily) returns, not annualized. Returns 0.0 if empty or zero-variance.
- `calculate_sortino_ratio(returns, risk_free_rate=0.04, periods_per_year=252) -> float` — downside denominator = `returns[returns < 0].std()`. Returns `float('inf')` when there are no/zero-variance downside returns but mean excess > 0; else 0.0.
- `calculate_profit_factor(trades: pd.DataFrame) -> float` — needs `net_pnl` column; `float('inf')` if gross_loss==0 and gross_profit>0.
- `generate_trade_analysis(closed_trades: list[dict]) -> pd.DataFrame` — passthrough DataFrame; adds `cumulative_pnl`, `is_winner` (if `net_pnl` present) and `cost_ratio = transaction_costs/abs(realized_pnl)` (if both present). Empty list → empty DataFrame.
- `generate_monthly_returns(equity_curve: list[dict]) -> pd.DataFrame` — REQUIRES both **`date`** and **`portfolio_value`** keys per row. Resamples month-end (`"M"`), pct_change, pivots to year×month with Jan–Dec column labels.

### Key conventions / gotchas
- Annualization factor for vol/Sharpe/Sortino is hardcoded **252** trading days; `volatility = returns.std()*sqrt(252)` for daily returns. Returns are SIMPLE (not log).
- `max_drawdown` is a positive magnitude in the report; `largest_loss`/`avg_loss` remain negative dollars.
- All return-type metrics are DECIMAL fractions except `ulcer_index` (percent points).
- `risk_free_rate` is an annual DECIMAL and is divided by `periods_per_year` internally — do not pre-annualize or pass a percent.
- Trades with exactly `net_pnl == 0` are counted in `total_trades` but in neither winners nor losers.

### Minimal usage example
```python
from engine.performance_metrics import calculate_performance_report

closed_trades = [
    {"net_pnl": 120.0, "realized_pnl": 135.0, "transaction_costs": 15.0, "hold_days": 7},
    {"net_pnl": -80.0, "realized_pnl": -68.0, "transaction_costs": 12.0, "hold_days": 4},
    {"net_pnl": 45.0,  "realized_pnl": 57.0,  "transaction_costs": 12.0, "hold_days": 10},
]
equity_curve = [
    {"date": "2026-01-02", "portfolio_value": 100_000.0},
    {"date": "2026-01-03", "portfolio_value": 100_120.0},
    {"date": "2026-01-06", "portfolio_value": 100_040.0},
    {"date": "2026-01-07", "portfolio_value": 100_085.0},
]
report = calculate_performance_report(
    closed_trades=closed_trades,
    equity_curve=equity_curve,
    initial_capital=100_000.0,
    risk_free_rate=0.04,   # annual decimal
)
print(report.sharpe_ratio, report.max_drawdown, report.win_rate)
print(report.to_dict())
```

---

## engine/event_gate.py

**Module path:** `vendor/swe/engine/event_gate.py`
**One-line purpose:** A HARD event-lockout pre-filter — answers "does a trade's holding window (with a per-event-kind buffer) touch a scheduled earnings/macro/dividend/split event for this ticker?" Returns a `(blocked, reason)` decision. This is the production gate the EV engine runs BEFORE ranking (cannot be bypassed by high EV).

### Import-time behavior / dependencies
- `import engine.event_gate` is LIGHTWEIGHT: stdlib only (`dataclasses`, `datetime`, `typing`). **No** numpy/pandas at import time. Pandas is imported lazily INSIDE `from_bloomberg_calendar` only (`import pandas as _pd`).
- IMPORTANT: `event_gate` is NOT re-exported by `engine/__init__.py`. You must import it directly: `from engine.event_gate import EventGate, ScheduledEvent`. (It is imported internally by `ev_engine.py`, `wheel_runner.py`, `wheel_tracker.py`, `data_connector.py`.)
- `EventKind` is a `typing.Literal[...]` of allowed kind strings: `"earnings"`, `"fomc"`, `"cpi"`, `"nfp"`, `"pce"`, `"ecb"`, `"boe"`, `"dividend"`, `"split"`, `"custom"`.

### Public dataclass: `ScheduledEvent`
Constructor `ScheduledEvent(ticker, kind, event_date, note="")`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `ticker` | `str` | (required) | underlying symbol, OR wildcard `"*"` meaning a macro event affecting EVERY candidate. Case-insensitive match. |
| `kind` | `EventKind` (str literal) | (required) | one of the allowed strings above; drives the buffer used |
| `event_date` | `datetime.date` | (required) | the scheduled event date (a `date`, not datetime) |
| `note` | `str` | `""` | free-form annotation (not used in matching) |

### Public dataclass: `EventGate`
Constructor (all defaults — `EventGate()` is valid):

| Field | Type | Default | Units |
|---|---|---|---|
| `earnings_buffer_days` | `int` | `5` | calendar days padded on BOTH sides of the trade window for `kind=="earnings"` |
| `macro_buffer_days` | `int` | `1` | days buffer for `fomc/cpi/nfp/pce/ecb/boe` |
| `dividend_buffer_days` | `int` | `1` | days buffer for `kind=="dividend"` |
| `split_buffer_days` | `int` | `3` | days buffer for `kind=="split"` |
| `events` | `list[ScheduledEvent]` | `[]` (default_factory) | the registered events |

Buffer mapping (`_buffer_for`): earnings→`earnings_buffer_days`; fomc/cpi/nfp/pce/ecb/boe→`macro_buffer_days`; dividend→`dividend_buffer_days`; split→`split_buffer_days`; anything else (e.g. `"custom"`)→`0`.

### Public methods
- `add_event(self, event: ScheduledEvent) -> None`
- `add_events(self, events: list[ScheduledEvent]) -> None`
- `clear(self) -> None`
- `is_blocked(self, ticker: str, trade_start: date, trade_end: date) -> tuple[bool, str]` — the primary query. An event matches when (a) it applies to the ticker (`event.ticker == "*"` OR case-insensitive equal) AND (b) `event_date` falls within `[trade_start - buf, trade_end + buf]` (buffer is per-kind, symmetric, inclusive). If no hits → `(False, "")`. If hits → `(True, reason)` where reason of the EARLIEST-dated hit is formatted exactly as:
  `f"event_lockout:{kind}@{event_date.isoformat()} (±{buf}d buffer)"` (e.g. `"event_lockout:earnings@2026-04-30 (±5d buffer)"`).
- `filter_candidates(self, candidates: list[dict], trade_start_key="trade_date", trade_end_key="expiration", ticker_key="ticker") -> tuple[list[dict], list[dict]]` — partitions into `(kept, blocked)`. Reads `candidate[ticker_key]`, `candidate[trade_start_key]`, `candidate[trade_end_key]`. Datetime values are normalized to `date` via `.date()`. If either start/end is `None`, the candidate is KEPT (not blocked). Each blocked candidate is returned as a shallow copy with an added `"event_lockout_reason"` field (originals not mutated).
- `@classmethod from_bloomberg_calendar(cls, earnings_df, macro_df=None, dividends_df=None, earnings_buffer_days=5, macro_buffer_days=1, dividend_buffer_days=1) -> EventGate` — builds a gate from DataFrames. Required columns: `earnings_df` → `ticker`, `announcement_date`; `macro_df` → `event`, `date` (event string lowercased; if not in fomc/cpi/nfp/pce/ecb/boe/custom it is coerced to `"custom"`, and macro events are added with `ticker="*"`); `dividends_df` → `ticker`, `ex_date` (added as `kind="dividend"`). Any df may be `None`. NaN/None dates are skipped.

### Sign conventions / gotchas
- Buffer is applied SYMMETRICALLY around the whole `[trade_start, trade_end]` window (start minus buf, end plus buf), inclusive on both ends.
- `kind="custom"` (and any unmapped kind) gets a **zero** buffer — a custom event only blocks if its date is literally inside `[trade_start, trade_end]`.
- Wildcard `"*"` ticker = macro event applies to all symbols. Ticker comparison is case-insensitive (`.upper()`).
- `is_blocked` returns the EARLIEST-dated matching event as the reason (sorts hits by `event_date`).
- Inputs MUST be `datetime.date` for `is_blocked` (it does no normalization); `filter_candidates` DOES normalize datetimes to date.

### Minimal usage example
```python
from datetime import date
from engine.event_gate import EventGate, ScheduledEvent

gate = EventGate(earnings_buffer_days=5, macro_buffer_days=1)
gate.add_event(ScheduledEvent("AAPL", "earnings", date(2026, 4, 30)))
gate.add_event(ScheduledEvent("*", "fomc", date(2026, 5, 1)))  # macro, all tickers

blocked, reason = gate.is_blocked(
    ticker="AAPL",
    trade_start=date(2026, 4, 25),
    trade_end=date(2026, 5, 20),
)
# blocked == True
# reason == "event_lockout:earnings@2026-04-30 (±5d buffer)"

kept, dropped = gate.filter_candidates(
    [{"ticker": "AAPL", "trade_date": date(2026, 4, 25), "expiration": date(2026, 5, 20)}]
)
# dropped[0]["event_lockout_reason"] == "event_lockout:earnings@2026-04-30 (±5d buffer)"
```

---

## engine/event_calendar.py

**Module path:** `vendor/swe/engine/event_calendar.py`
**One-line purpose:** A richer event-calendar data structure + builders (earnings/dividends CSV, hardcoded/JSON FOMC/CPI/NFP/GDP/expiry dates) and an `EventRiskFilter` that answers "should I avoid this trade given events before expiry?" plus event-based sizing and IV-premium adjustments.

### Import-time behavior / dependencies
- `import engine.event_calendar` imports **`pandas`** at module top (plus stdlib `dataclasses`, `datetime`, `enum`). No numpy/scipy/etc. `json`/`pathlib` are imported lazily inside `CalendarIngestionManager` methods.
- All these symbols ARE re-exported by `engine/__init__.py` (`EventCalendar`, `EventCalendarBuilder`, `EventImpact`, `EventRiskFilter`, `EventType`, `MarketEvent`, `build_default_calendar`) — but importing through the package triggers the full heavy engine import. Prefer `from engine.event_calendar import ...` for a light import.

### Public enums
- `EventType(Enum)`: `EARNINGS="earnings"`, `DIVIDEND_EX="dividend_ex"`, `DIVIDEND_PAY="dividend_pay"`, `FOMC="fomc"`, `CPI="cpi"`, `NFP="nfp"`, `GDP="gdp"`, `OPTIONS_EXPIRY="options_expiry"`, `STOCK_SPLIT="stock_split"`, `OTHER="other"`.
- `EventImpact(Enum)`: `LOW="low"`, `MEDIUM="medium"`, `HIGH="high"`, `CRITICAL="critical"`.

### Public dataclass: `MarketEvent`
Constructor `MarketEvent(event_date, event_type, symbol, description, impact=EventImpact.MEDIUM, ...)`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `event_date` | `datetime.date` | (required) | event date |
| `event_type` | `EventType` | (required) | enum |
| `symbol` | `str \| None` | (required, may be None) | ticker; **`None` == a MACRO event** (applies to all) |
| `description` | `str` | (required) | human text |
| `impact` | `EventImpact` | `EventImpact.MEDIUM` | severity |
| `expected_move` | `float \| None` | `None` | expected % move as a DECIMAL fraction (e.g. 0.05 = ±5%); used by IV premium calc |
| `historical_move` | `float \| None` | `None` | avg historical move (decimal) |
| `time_of_day` | `str \| None` | `None` | `"pre"`, `"post"`, or `"during"` |
| `dividend_amount` | `float \| None` | `None` | dollars per share |
| `dividend_yield` | `float \| None` | `None` | yield (decimal) |

Members: `__str__` gives `"YYYY-MM-DD [SYMBOL or MACRO] type: description"`. Property `days_until -> int` = `(event_date - date.today()).days` (uses TODAY's system date — non-deterministic).

### Public dataclass: `EventCalendar`
Constructor `EventCalendar()` (all fields default). Public fields: `events: list[MarketEvent] = []`. (Two `_`-prefixed index dicts are internal but technically dataclass fields.)

Public methods:
- `add_event(event: MarketEvent) -> None` / `add_events(events: list[MarketEvent]) -> None` — also build internal date/symbol indices.
- `get_events_in_range(start_date: date, end_date: date, symbol: str|None=None, event_types: list[EventType]|None=None) -> list[MarketEvent]` — inclusive date filter, sorted by date. GOTCHA: when `symbol` is given, macro events (symbol is None) are ALSO included (only other-symbol events are excluded).
- `get_events_for_symbol(symbol: str, start_date=None, end_date=None) -> list[MarketEvent]` — uses the symbol index (does NOT include macro/None-symbol events), sorted by date.
- `get_next_event(symbol: str, from_date: date, event_types=None) -> MarketEvent | None` — first symbol event on/after `from_date`.
- `days_to_next_earnings(symbol: str, from_date: date) -> int | None` — days to next `EARNINGS` for symbol (None if none).
- `days_to_next_dividend(symbol: str, from_date: date) -> int | None` — days to next `DIVIDEND_EX`.
- `has_event_before_expiry(symbol: str, trade_date: date, expiry_date: date, event_types=None) -> tuple[bool, list[MarketEvent]]` — events in `[trade_date, expiry_date]` for the symbol; ALWAYS also unions in FOMC/CPI/NFP macro events in range regardless of `event_types`. Returns `(has_event, all_events)`. THIS is the calendar's "is timestamp inside a window" query — note it works on date RANGES, not a single timestamp, and has NO buffer (use `EventGate` or `EventRiskFilter` for buffered logic).

### Public class: `EventRiskFilter`
Constructor `EventRiskFilter(calendar: EventCalendar, earnings_buffer_days: int = 5, fomc_buffer_days: int = 2, dividend_buffer_days: int = 1)`. REQUIRES a populated `EventCalendar`.

- `should_avoid_trade(symbol: str, trade_date: date, expiry_date: date) -> tuple[bool, str]` — returns `(should_avoid, reason)`. Avoids if next earnings is on/before expiry AND `days_to_earnings <= earnings_buffer_days`; OR any FOMC in `[trade_date, expiry_date]` with `days_to_fomc <= fomc_buffer_days`. GOTCHA: buffer here is one-sided (days from `trade_date` forward only), unlike `EventGate`'s symmetric buffer.
- `get_event_adjusted_sizing(symbol, trade_date, expiry_date, base_size: float) -> tuple[float, str]` — multiplies `base_size` by 0.5 per EARNINGS, 0.8 per FOMC, 0.95 per DIVIDEND_EX in the window (multiplicative, compounding). Returns `(adjusted_size, reason)`.
- `get_event_premium_adjustment(symbol, trade_date, expiry_date) -> float` — IV-premium MULTIPLIER (1.0 = no adjustment). Per EARNINGS: `*(1 + expected_move)` if `expected_move` set, else `*1.35`; per FOMC: `*1.10`. Considers only EARNINGS/FOMC.

### Public class: `EventCalendarBuilder` (all static methods returning `list[MarketEvent]`)
- `from_earnings_csv(filepath: str)` — CSV cols: `symbol`, `date`, optional `time`(pre/post), `expected_move`, `quarter`. Missing file → `[]`. Impact set HIGH.
- `from_dividends_csv(filepath: str)` — CSV cols: `symbol`, `ex_date`, `pay_date`, `amount`, optional `yield`. Emits DIVIDEND_EX (MEDIUM) + DIVIDEND_PAY (LOW).
- `generate_fomc_dates(year: int)` — HARDCODED dates for 2024/2025/2026 only (8 each, HIGH, time `"during"`); other years → `[]`.
- `generate_cpi_dates(year: int)` — HARDCODED 2024/2025/2026 (12 each, HIGH, time `"pre"`); other years → `[]`.
- `generate_nfp_dates(year: int)` — COMPUTED first-Friday-of-month (12, HIGH, `"pre"`); works for any year.
- `generate_gdp_dates(year: int)` — COMPUTED ~25th of Jan/Apr/Jul/Oct adjusted to weekday (4, MEDIUM, `"pre"`).
- `generate_monthly_expiries(year: int, symbols: list[str]|None=None)` — third Friday each month (12, MEDIUM, symbol=None). `symbols` param is accepted but UNUSED.
- `from_dataframe(df, date_col="date", symbol_col="symbol", type_col="event_type", description_col="description")` — generic loader; unknown type strings → `EventType.OTHER`; reads optional `impact` column.

### Public functions
- `validate_calendar_dates(calendar: EventCalendar, year: int) -> dict[str, list[str]]` — returns `{"warnings": [...], "errors": [...]}`; checks counts (FOMC=8, CPI=12, NFP=12, expiries=12).
- `build_default_calendar(years: list[int], earnings_file: str|None=None, dividends_file: str|None=None, include_macro_events: bool=True, validate: bool=True, calendar_dir: str|None=None, check_staleness: bool=True, strict: bool=False) -> EventCalendar` — the convenience constructor. Loads FOMC/CPI/NFP/GDP from authoritative JSON in `calendar_dir` (default `config/calendars/` relative to repo) if present, else falls back to hardcoded/computed dates. GOTCHAS: it PRINTS staleness/validation warnings to stdout; raises `ValueError` if validation produces errors; `strict=True` raises `ValueError` when an authoritative JSON file is missing instead of falling back. Earnings/dividends are loaded only if `earnings_file`/`dividends_file` paths are given (calendar has NO earnings unless you supply them).

### Other (non-exported) helper classes in the file
- `CalendarSourceConfig` — static metadata (`SOURCES`, `VERIFIED_YEARS`).
- `CalendarIngestionManager(calendar_dir: str|None=None)` — JSON load/save/staleness; used internally by `build_default_calendar`. Not re-exported from the package.

### Conventions / gotchas vs. event_gate
- `EventCalendar` matching is by exact date RANGE with NO buffer; buffered avoidance lives in `EventRiskFilter` (one-sided forward buffer) and in the separate `EventGate` (symmetric buffer, the production hard gate).
- Macro events use `symbol=None` here (vs. `ticker="*"` in `event_gate`).
- `expected_move` is a decimal fraction (0.05 = 5%), consumed directly in `get_event_premium_adjustment` as `(1 + expected_move)`.
- Earnings dates are NEVER auto-generated — they only enter via CSV (`from_earnings_csv`) or `from_dataframe`. FOMC/CPI are hardcoded only for 2024–2026.

### Minimal usage example
```python
from datetime import date
from engine.event_calendar import (
    EventCalendar, EventRiskFilter, MarketEvent, EventType, EventImpact,
)

cal = EventCalendar()
cal.add_event(MarketEvent(
    event_date=date(2026, 4, 30),
    event_type=EventType.EARNINGS,
    symbol="AAPL",
    description="AAPL Q2 Earnings",
    impact=EventImpact.HIGH,
    expected_move=0.06,           # decimal fraction (6%)
    time_of_day="post",
))

# "is there an event between trade and expiry?" (no buffer)
has_evt, evts = cal.has_event_before_expiry(
    symbol="AAPL", trade_date=date(2026, 4, 20), expiry_date=date(2026, 5, 15),
)  # -> (True, [MarketEvent(... AAPL earnings ...)])

# buffered avoidance decision
risk = EventRiskFilter(cal, earnings_buffer_days=5)
avoid, reason = risk.should_avoid_trade("AAPL", date(2026, 4, 28), date(2026, 5, 15))
# avoid == True, reason like "Earnings in 2 days (within buffer)"

prem_mult = risk.get_event_premium_adjustment("AAPL", date(2026, 4, 20), date(2026, 5, 15))
# == 1.06  (1 + expected_move)
```

**Cross-module note for the caller:** there are TWO distinct lockout mechanisms. `engine.event_gate.EventGate` is the lightweight, stdlib-only, SYMMETRIC-buffer HARD gate (returns `event_lockout:...` reason strings; NOT re-exported by the package) used in the EV decision path. `engine.event_calendar` (pandas-backed, re-exported) is the richer calendar with `EventRiskFilter` (ONE-SIDED buffer) for sizing/premium/avoidance. They use different macro conventions (`ticker="*"` vs `symbol=None`) and are not interchangeable — pick the one matching your needs and feed it dates from your own data source (no earnings dates ship by default).

---

## engine/risk_manager.py

**Module path:** `vendor/swe/engine/risk_manager.py` (import as `engine.risk_manager` with `vendor/swe` on `sys.path`).
**One-line purpose:** Institutional risk controls for an options/wheel portfolio — position sizing (fixed-fractional, Kelly, vol-scaled, equal-risk, max-loss), portfolio Greeks aggregation, parametric/historical/covariance/Monte-Carlo VaR & CVaR, drawdown scaling, concentration/sector limits, and HRP weighting.

### Import-time behavior
- Top-level imports run at module import: `numpy as np`, `pandas as pd`, `from scipy import stats`, and `from .option_pricer import black_scholes_all_greeks`. **So importing this module REQUIRES numpy, pandas, scipy, and pulls in `engine.option_pricer`** (which itself requires numpy/scipy). statsmodels/arch/sklearn/requests are NOT required.
- `from .policy_config import load_policy` is imported lazily inside `RiskManager.from_policy` only.
- `scipy.cluster.hierarchy` / `scipy.spatial.distance` are imported lazily inside HRP methods only.
- **Caveat about `from engine import risk_manager`:** `engine/__init__.py` performs heavy eager imports of the WHOLE engine (ev_engine, wheel_runner, monte_carlo, volatility_surface, portfolio_tracker, etc.). To avoid that, import the submodule directly (`import engine.risk_manager` still triggers the package `__init__`; to truly avoid it, add `vendor/swe/engine` to path or import the file module directly). The clean public re-exports from `engine` are listed in `engine/__init__.py` `__all__`.

### Enums

`PositionSizingMethod(Enum)` — string values:
- `FIXED_FRACTIONAL = "fixed_fractional"`, `KELLY = "kelly"`, `VOLATILITY_SCALED = "volatility_scaled"`, `EQUAL_RISK = "equal_risk"`, `MAX_LOSS = "max_loss"`.

### Dataclasses

`@dataclass PortfolioGreeks` — aggregated portfolio Greeks. All fields default `0.0`:
- `delta: float = 0.0` — net delta exposure in **shares** (per-share delta × contracts × 100, sign-adjusted; short = negative).
- `gamma: float = 0.0` — net gamma (delta per $1), same multiplier.
- `theta: float = 0.0` — **daily** time decay in $/day (pricer's annual theta divided by 365).
- `vega: float = 0.0` — $ per **1 vol point** (1 percentage-point IV move), aggregated × contracts × 100.
- `rho: float = 0.0` — $ per 1 percentage-point rate move × contracts × 100.
- `delta_dollars: float = 0.0` — `delta_per_share × multiplier × spot`.
- `gamma_dollars: float = 0.0` — `gamma_per_share × multiplier × spot² / 100`.
- `__str__` provided. No constructor args required (all defaulted).

`@dataclass RiskLimits` — limit config. Fields + defaults:
- `max_positions: int = 10`
- `max_single_position_pct: float = 0.20` (decimal fraction; 0.20 = 20%)
- `max_sector_pct: float = 0.40`
- `max_portfolio_delta: float = 0.50` (max |delta_dollars/NAV|, decimal)
- `max_portfolio_gamma_dollars: float = 50000` (dollar gamma)
- `max_portfolio_vega: float = 10000` (dollar vega)
- `max_drawdown_pct: float = 0.20`
- `daily_loss_limit_pct: float = 0.03` ← **this is the daily-loss / kill-switch knob** (decimal, 3%). There is NO constant literally named `MAX_DAILY_LOSS`; the field is `daily_loss_limit_pct`. NOTE: `check_limits` does NOT currently test daily loss; this field is the configured limit you must enforce yourself (or use `update_portfolio_value` + your own check). `from_policy` maps it from `policy.risk.max_daily_loss_pct`.
- `max_var_95_pct: float = 0.05`
- `max_cvar_95_pct: float = 0.08`
- `min_positions_for_full_size: int = 5`
- `correlation_penalty_threshold: float = 0.70`

`@dataclass RiskMetrics` — computed output of `get_risk_metrics`. Fields/defaults:
- `var_95_1d: float=0.0`, `var_99_1d: float=0.0`, `cvar_95_1d: float=0.0` (positive dollar amounts)
- `current_drawdown: float=0.0`, `max_drawdown: float=0.0`, `drawdown_duration_days: int=0`
- `herfindahl_index: float=0.0` (0–1), `largest_position_pct: float=0.0`
- `portfolio_greeks: PortfolioGreeks = field(default_factory=PortfolioGreeks)`
- `available_risk_budget: float = 1.0` (0–1)

`@dataclass SectorExposure`:
- `sector: str`, `position_count: int`, `notional_exposure: float`, `exposure_pct: float` (decimal % of NAV), `symbols: list[str]`. All positional/required.
- `@property is_concentrated -> bool` → True when `exposure_pct > 0.25` (hard-coded 25%, independent of the manager's `max_sector_pct`).

### Class `RiskManager`

Constructor:
```python
RiskManager(
    limits: RiskLimits | None = None,
    sizing_method: PositionSizingMethod = PositionSizingMethod.VOLATILITY_SCALED,
    risk_free_rate: float = 0.05,                 # annual, DECIMAL (0.05 = 5%)
    allow_heuristic_var_fallback: bool = True,
    concentrated_book_threshold: int = 3,
    environment: str = "dev",                     # 'dev'|'staging'|'prod'
)
```
- Gotcha: if `environment == "prod"`, `allow_heuristic_var_fallback` is FORCED to `False` regardless of the argument (hard governance control). Default `sizing_method` is VOLATILITY_SCALED, not Kelly.
- Alt constructor `@classmethod RiskManager.from_policy(environment: str = "dev") -> RiskManager` — reads thresholds from `engine.policy_config.load_policy()` (requires that module/config to import successfully).

Key public methods:

`calculate_position_size(self, portfolio_value: float, underlying_price: float, strike: float, iv: float, dte: int, win_probability: float = 0.70, avg_win: float = 1.0, avg_loss: float = 1.0, existing_positions: int = 0, underlying_correlation: float = 0.0) -> tuple[int, str]`
- Returns `(contracts: int, reasoning: str)`. `iv` is annualized decimal; `strike`/`underlying_price` in dollars; `portfolio_value` in dollars. Multiplier hard-coded 100 shares/contract. Notional per contract = `strike * 100`.
- KELLY branch here uses **half-Kelly internally** (`kelly * 0.5`) then caps at 0.25; win/loss ratio `b = avg_win/avg_loss`. VOLATILITY_SCALED uses baseline IV 0.20 and caps scalar at 2×. Applies concentration scalar (floor 0.5), correlation penalty (floor 0.3, triggers above `correlation_penalty_threshold`), and drawdown scalar. Final pct capped at `max_single_position_pct`. Returns `(0, reason)` if `existing_positions >= max_positions`.

`calculate_portfolio_greeks(self, positions: list[dict], spot_prices: dict[str, float]) -> PortfolioGreeks`
- Each position dict keys: `symbol`, `strike`, `dte` (days; converted `T = dte/365`), `iv` (annual decimal), `option_type` ('call'/'put'), `contracts`, optional `is_short` (default True), optional `dividend_yield` (default 0.0), optional `underlying_price` (fallback 100 if symbol not in `spot_prices`).
- **Sign convention:** `direction = -1 if is_short else 1`; `multiplier = direction * contracts * 100`. Short positions produce NEGATIVE delta/gamma/theta/vega/rho exposure. Theta divided by 365 → DAILY. `r = self.risk_free_rate`.

`calculate_var(self, portfolio_value, positions, spot_prices, returns_data: pd.DataFrame|None=None, volatilities: dict[str,float]|None=None, correlation_matrix: pd.DataFrame|None=None, confidence: float=0.95, horizon_days: int=1) -> tuple[float, float]`
- Returns `(VaR, CVaR)` as POSITIVE dollar amounts. Method priority: (1) covariance VaR if both `correlation_matrix` and `volatilities` given; (2) historical sim if `returns_data` has >30 rows; (3) parametric delta-gamma-vega fallback.
- **Governance gotcha:** for a "concentrated" book (`len(unique symbols) < concentrated_book_threshold`) when fallback is blocked, it WARNS and returns `(0.0, 0.0)` (logs to `self._var_governance_log`) — silent zero VaR, not an exception. Always blocked in `prod`.

`calculate_covariance_var(self, portfolio_value, positions, spot_prices, volatilities: dict[str,float], correlation_matrix: pd.DataFrame, confidence=0.95, horizon_days=1, vol_of_vol=0.05) -> tuple[float, float, dict]`
- `volatilities` are ANNUALIZED decimals; internally /√252 to daily then ×√horizon. `correlation_matrix` must be a DataFrame indexed/columned by symbol (sub-selected via `.loc[symbols, symbols]`; PSD-repaired). Third return is component breakdown (`delta_var`, `gamma_var`, `vega_var`, `per_asset_contribution`).

`calculate_monte_carlo_var(self, portfolio_value, positions, spot_prices, volatilities, correlation_matrix=None, confidence=0.95, horizon_days=1, n_simulations=10000, vol_of_vol=0.10, include_jump_diffusion=False, jump_intensity=2.0, jump_mean=-0.02, jump_std=0.03, seed=None) -> tuple[float, float, dict]`
- Full revaluation; correlated GBM via Cholesky; optional Merton jumps. `dt = horizon_days/252`. Returns `(VaR, CVaR, details_dict)` (positive dollars).

`update_portfolio_value(self, value: float) -> None` — appends to NAV history, updates `peak_value`, appends a return to `returns_history`. Call this each step to drive drawdown scaling in sizing.

`get_risk_metrics(self, portfolio_value, positions, spot_prices) -> RiskMetrics` — computes greeks, 95%/99% VaR, drawdown (needs prior `update_portfolio_value` calls for `peak_value`), Herfindahl, risk budget.

`run_stress_tests(self, portfolio_value, positions, spot_prices, custom_scenarios: list[dict]|None=None) -> dict[str, dict]` — standard crash/vol/gap/rate/worst-case scenarios. Each result: `{pnl, pct_loss, description, components}`. **Units in P&L formulas:** `delta_pnl = delta_dollars * spot_move` (spot_move decimal), `gamma_pnl = 0.5 * gamma_dollars * spot_move²`, `vega_pnl = vega * vol_move * 100` (vol_move decimal → ×100 to vol points), `rho_pnl = rho * rate_move * 100`, `theta_pnl = theta * days`. Custom scenario dict keys: `spot_move`, `vol_move`, `rate_move`, `description` (all decimals).

`check_limits(self, portfolio_value, positions, spot_prices, proposed_trade: dict|None=None) -> tuple[bool, list[str]]` — returns `(is_within_limits, violations)`. Checks position count, single-position %, drawdown, 95% VaR %, delta %, |gamma_dollars|, |vega|. Does NOT check daily-loss or sector. (`proposed_trade` is accepted but not used in the body.)

### Module-level functions (the Kelly + sizing API)

`calculate_kelly_fraction(win_rate: float, avg_win: float, avg_loss: float, kelly_fraction: float = 0.5) -> float`
- **This is the primary Kelly entry point.** `win_rate` ∈ [0,1] (returns 0.0 if outside). `avg_win`, `avg_loss` are positive magnitudes (returns 0.0 if either ≤ 0). Full Kelly `f* = (p·b − q)/b`, `b = avg_win/avg_loss`. The `kelly_fraction` param is the **fractional-Kelly multiplier** (0.5 = half-Kelly, default). **Result is bounded to `[0.0, 0.25]`** (hard cap at 25% of capital). Returns a capital fraction (decimal), NOT contracts.

`calculate_optimal_contracts(capital: float, strike: float, max_risk_pct: float = 0.05, margin_requirement: float = 0.20, stress_loss_pct: float = 0.25, premium_per_share: float = 0.0) -> int`
- Stress-loss sizing for a cash-secured short put. `notional_per_contract = strike*100`; loss-per-contract = `max((strike*stress_loss_pct − premium_per_share)*100, strike*0.10*100)` (10%-of-notional floor). Returns `min(contracts_by_risk, contracts_by_margin)`, floored at 0. **Does NOT force a minimum of 1** — returns 0 when constraints can't be met. All money in dollars, `premium_per_share` per-share dollars.

`DEFAULT_SECTOR_MAP: dict[str,str]` — GICS sector map for ~130 S&P 500 tickers (`get_sector` returns `"Unknown"` for misses).

### Class `SectorExposureManager`
`__init__(self, sector_map: dict[str,str]|None=None, max_sector_pct: float=0.25)`. Position dicts need `symbol`, `strike`, `contracts` (malformed rows silently skipped). `notional = strike*100*contracts`.
- `get_sector(symbol) -> str`; `calculate_sector_exposures(positions, portfolio_value) -> dict[str, SectorExposure]`; `check_sector_limit(symbol, proposed_notional, positions, portfolio_value) -> tuple[bool, str]`; `get_sector_violations(...) -> list[str]`; `suggest_diversification(positions, portfolio_value, available_symbols) -> list[str]`.

### Class `HierarchicalRiskParity` (López de Prado 2016)
`__init__(self, linkage_method: str = "ward")`. `fit(self, returns: pd.DataFrame, covariance: pd.DataFrame|None=None) -> dict[str,float]` (returns symbol→weight summing to 1). Lazily imports `scipy.cluster.hierarchy`/`scipy.spatial.distance`. Convenience: `calculate_hrp_weights(returns_df, target_symbols=None) -> dict`; `optimize_position_weights(symbols, returns_data, max_weight=0.20, min_weight=0.02) -> dict` (HRP + per-name caps, renormalized).

### Sign conventions & gotchas (must-know)
- **Short = negative Greeks.** `direction=-1` for `is_short=True` (the default). Every Greek and dollar-Greek for a short option is negative.
- **Theta is DAILY** in `PortfolioGreeks` (pricer returns annual, divided by 365).
- **Vega/Rho are per 1 vol/rate POINT.** When applying a decimal shock (e.g. 0.05 IV), multiply by 100 (this module does it consistently: `vega * vol_move * 100`). This is the #1 unit error per `docs/GREEKS_UNIT_CONTRACT.md`.
- **`gamma_dollars` is divided by 100** by convention (`gamma * spot² * mult / 100`).
- VaR annualized vols → daily via `/√252`; horizon scaling `×√horizon_days` (price) and proper compounding in historical VaR. (Note: VaR uses 252, but `calculate_portfolio_greeks` uses `dte/365` for T and theta /365.)
- **Concentrated-book VaR can silently return `(0,0)`** in prod or when `allow_heuristic_var_fallback=False` — check `_var_governance_log` / catch warnings; supply `correlation_matrix`+`volatilities` or `returns_data` to get a real number.
- `risk_free_rate` is a DECIMAL annual rate (default 0.05). Multiplier is hard-wired to 100 shares/contract everywhere.

### Minimal usage example
```python
import sys; sys.path.insert(0, r"C:\Users\merty\Desktop\Day-Trading-Bot\vendor\swe")
import pandas as pd
from engine.risk_manager import (
    RiskManager, RiskLimits, PositionSizingMethod,
    calculate_kelly_fraction, calculate_optimal_contracts,
)

# Fractional-Kelly capital fraction (half-Kelly), capped at 25%
f = calculate_kelly_fraction(win_rate=0.70, avg_win=1.0, avg_loss=1.0, kelly_fraction=0.5)

# Stress-loss contract count for a cash-secured put
n = calculate_optimal_contracts(capital=100_000, strike=150.0,
                                max_risk_pct=0.05, premium_per_share=2.50)

rm = RiskManager(limits=RiskLimits(daily_loss_limit_pct=0.03),
                 sizing_method=PositionSizingMethod.KELLY,
                 risk_free_rate=0.05, environment="dev")

positions = [{"symbol": "AAPL", "option_type": "put", "strike": 150.0,
              "dte": 30, "iv": 0.25, "contracts": 2, "is_short": True}]
spot = {"AAPL": 155.0}

greeks = rm.calculate_portfolio_greeks(positions, spot)   # PortfolioGreeks (short -> negatives)
rm.update_portfolio_value(100_000)                         # feed NAV for drawdown logic
var95, cvar95 = rm.calculate_var(100_000, positions, spot,
                                 returns_data=pd.DataFrame({"returns": [ -0.01, 0.02, ... ]}))
ok, violations = rm.check_limits(100_000, positions, spot)
contracts, why = rm.calculate_position_size(
    portfolio_value=100_000, underlying_price=155.0, strike=150.0,
    iv=0.25, dte=30, win_probability=0.7, existing_positions=len(positions))
```

### Non-obvious caller requirements
- Position dicts MUST carry `symbol, strike, dte, iv, option_type, contracts`; optional `is_short` (default True!), `dividend_yield`, `underlying_price`. If you mean a LONG option you must set `is_short=False`.
- Provide annualized-decimal IVs and a decimal `risk_free_rate`. Multiplier 100 is assumed (no contract-multiplier override).
- For multi-asset VaR, `correlation_matrix` must be a `pd.DataFrame` indexed/columned by the same symbols, and `volatilities` a dict symbol→annual decimal.
- The daily-loss kill-switch is a CONFIG VALUE (`RiskLimits.daily_loss_limit_pct`), not an enforced check — wire your own intraday loss guard against it.

---

## backtests/simulator.py

**Module path:** `vendor/swe/backtests/simulator.py` (import as `backtests.simulator`).
**One-line purpose:** A simplified, **daily** (date-grouped, not event-driven) backtester for the wheel strategy that drives a `WheelTracker`, using constant-IV Black-Scholes reconstruction to estimate option values for profit-target/stop-loss exits. Explicitly labeled a PLACEHOLDER pending daily option-price data.

### Import-time behavior
- Top of file mutates path: `sys.path.append(str(Path(__file__).parent.parent))` (adds `vendor/swe` so `engine.*` resolves).
- Imports at module load: `pandas as pd`, `numpy as np` (np imported but effectively unused), `datetime`, `pathlib.Path`, `sys`, plus `from engine.wheel_tracker import WheelTracker, PositionState` and `from engine.option_pricer import estimate_option_price_from_iv`.
- **Importing this transitively imports `engine.wheel_tracker` and `engine.option_pricer`**, which require numpy/scipy/pandas. (`engine.wheel_tracker` is large; it may pull additional engine deps.) No statsmodels/arch/sklearn/requests required by this file directly.
- `backtests/__init__.py` ALSO eagerly imports `walk_forward` (WalkForwardValidator etc.) — `import backtests.simulator` triggers the package `__init__` and loads walk_forward too.

### The base class: `WheelBacktester`

This is the class to extend into an event-driven intraday replay. **It is daily/granular, not bar/tick event-driven** — there is no event queue; it groups trades by calendar date and processes one day at a time. To make it intraday you would override the loop and the exit-pricing.

Constructor:
```python
WheelBacktester(
    initial_capital: float = 100000.0,
    profit_target_pct: float = 0.60,    # close at 60% of max profit (premium captured)
    stop_loss_multiple: float = 2.0,    # close when option value >= 2x entry premium
    max_positions: int = 10,
    risk_free_rate: float = 0.04,       # annual decimal
)
```
Instance attributes created: `self.tracker = WheelTracker(initial_capital)` (NOTE: constructed with ONLY initial_capital — all EV-authority/sector/single-name/delta/kelly caps default to False, `require_ev_authority=False`), `self.profit_target_pct`, `self.stop_loss_multiple`, `self.max_positions`, `self.risk_free_rate`, and `self.trade_log: list` (append-only audit list of dict rows).

### Main loop / how bars/events are fed

`run_backtest(self, trade_universe: pd.DataFrame, ohlcv_data: dict, start_date: str, end_date: str)`
- This is the entry point and the "main loop". It is **NOT** event-driven; it:
  1. Filters `trade_universe` rows to `[start_date, end_date]` (string compare on a `date` column), sorts by `date`.
  2. `for trade_date, day_trades in trades.groupby('date'): self._process_day(trade_date, day_trades, ohlcv_data)` — one iteration per calendar date that has candidate trades.
  3. After the loop, marks to market on `end_date` and returns `self.tracker.get_performance_summary()` (a `pd.DataFrame`).
- **Inputs / data feed shapes:**
  - `trade_universe: pd.DataFrame` — candidate trades. Required columns used: `date`, `ticker`, `strategy_leg` (`'short_put'` or `'covered_call'`), `strike`, `mid_price` (premium per share), `implied_vol` (annual decimal), `expiration` (parseable date), `dte`; optional `expected_value` (used to rank puts if present).
  - `ohlcv_data: dict[str, pd.DataFrame]` — per-ticker price frames, each with columns `date` (Python `date` objects to match `current_date`) and `close`. Prices in dollars.
  - `start_date`/`end_date` are `'YYYY-MM-DD'` strings.

### The per-day pipeline (override points)

`_process_day(self, current_date, day_trades, ohlcv_data)` — the per-step orchestrator, runs in this order:
1. `_check_exits(current_date, ohlcv_data)` — exit/expiration/assignment management.
2. `_manage_stock_positions(current_date, day_trades, ohlcv_data)` — sell covered calls on owned stock (wheel completion).
3. `if len(self.tracker.positions) < self.max_positions: _enter_new_trades(...)` — open new short puts.
4. `current_prices = self._get_current_prices(current_date, ohlcv_data)` then `self.tracker.mark_to_market(current_date, current_prices, self.risk_free_rate)`.

`_check_exits(self, current_date, ohlcv_data)` — iterates `self.tracker.positions.items()`. Branches on `pos.state.value`:
- `'short_put'`: if past `pos.put_expiration_date` → `handle_put_assignment` (if `price < put_strike`) else `handle_put_expiration`. Otherwise reconstructs value via `estimate_option_price_from_iv(underlying_price, strike=pos.put_strike, dte=days_remaining, iv=pos.put_entry_iv, risk_free_rate=self.risk_free_rate, option_type='put')`. Profit target: `current_profit = put_premium - estimated_value >= profit_target_pct * put_premium` → `close_short_put(..., "profit_target")`. Stop: `estimated_value >= stop_loss_multiple * put_premium` → `close_short_put(..., "stop_loss")`.
- `'covered_call'`: symmetric logic with `call_*` fields, appends to `self.trade_log`.
- `'stock_owned'`: no-op (handled in step 2).

`_manage_stock_positions(...)` — for `PositionState.STOCK_OWNED` positions, finds `day_trades` where `strategy_leg=='covered_call'` and `strike > current_price`, picks highest `mid_price`, calls `self.tracker.open_covered_call(ticker, strike, premium, entry_date, expiration_date, iv)`.

`_enter_new_trades(...)` — filters `strategy_leg=='short_put'`, sorts by `expected_value` (if column present) else `mid_price` descending, opens via `self.tracker.open_short_put(ticker, strike, premium, entry_date, expiration_date, iv)` until `max_positions`; skips tickers already in `self.tracker.positions`.

`_get_current_prices(self, current_date, ohlcv_data) -> dict[str, float]` — `{ticker: close}` for rows matching `current_date`.

### How fills / PnL are recorded
- There is **no explicit fill/slippage model in this class** — fills are assumed at the candidate's `mid_price` (entry premium) and at the BS-reconstructed `estimated_*_value` (exit). The module docstring states "mid-price fills with fixed slippage" but the simulator body applies NO slippage/commission — costs would come from inside `WheelTracker` if enabled (caps default off here).
- **PnL and cash are owned by `WheelTracker`, not the simulator.** The simulator only calls tracker methods; the tracker maintains cash, positions, `closed_positions`, and computes P&L. Multiplier 100 shares/contract is inside the tracker.
- `self.trade_log` is a plain list of dict events (open/close with date, ticker, action, prices, reason) the simulator appends for audit; it is separate from the tracker's accounting.
- Final result = `self.tracker.get_performance_summary() -> pd.DataFrame` (empty DataFrame if no closed positions).

### Extension points for an event-driven intraday replay
- **Override `run_backtest`** to replace `groupby('date')` with your intraday bar/event loop (e.g. iterate `(timestamp, bar)` events), calling a per-event analogue of `_process_day`.
- **Override `_check_exits`** to use real intraday option quotes instead of `estimate_option_price_from_iv` constant-IV reconstruction (the file's stated limitation).
- **Override `_get_current_prices`** to map your bar's price into `{ticker: price}` for `mark_to_market`.
- Reuse `WheelTracker` as the position/PnL ledger, or swap in your own tracker — the simulator only depends on this tracker surface:
  - `WheelTracker(initial_capital)`; `.positions: dict[ticker, WheelPosition]`; `.mark_to_market(current_date: date, prices: dict[str,float], risk_free_rate: float=0.04, current_ivs: dict|None=None) -> float`; `.get_performance_summary() -> pd.DataFrame`.
  - `.open_short_put(ticker, strike, premium, entry_date: date, expiration_date: date, iv, ev_authority_token=None, current_ev_dollars=None, prob_profit=None) -> bool`
  - `.open_covered_call(ticker, strike, premium, entry_date: date, expiration_date: date, iv, ev_authority_token=None, current_ev_dollars=None) -> bool`
  - `.close_short_put(ticker, buyback_price, exit_date: date, reason='early_exit') -> dict|None`
  - `.close_covered_call(ticker, buyback_price, exit_date: date, reason='early_exit') -> dict|None`
  - `.handle_put_assignment(ticker, assignment_date: date, stock_price) -> bool`; `.handle_put_expiration(ticker, expiry_date: date, stock_price) -> dict|None`
  - `.handle_call_assignment(ticker, assignment_date: date) -> dict|None`; `.handle_call_expiration(ticker, expiry_date: date, stock_price) -> bool`
  - WheelPosition fields read by the simulator: `state` (`PositionState` enum; `.value` ∈ {`'short_put','covered_call','stock_owned'}`), `put_strike`, `put_premium`, `put_entry_iv`, `put_expiration_date`, `put_dte_at_entry`, and `call_*` analogues.

### Sign / unit conventions & gotchas
- `profit_target_pct` is a fraction of MAX profit (the premium), not an absolute dollar target. Exit fires when `premium - current_value >= profit_target_pct * premium`.
- `stop_loss_multiple` is a MULTIPLE of entry premium on the option's VALUE (2.0 ⇒ exit when the option you sold has doubled in price against you).
- Dates: `ohlcv_data[...]['date']` and `current_date` must be the SAME type (Python `date`) for equality matching; `expiration`/`end_date` are parsed via `pd.to_datetime(...).date()`. `entry_date`/`expiration_date` passed to tracker must be `datetime.date`.
- `iv` is annualized decimal; `risk_free_rate` annual decimal (default 0.04, differs from RiskManager's 0.05 default); `dte` in days; `estimate_option_price_from_iv` returns price PER SHARE (×100 happens in the tracker).
- Constant-IV assumption is the central limitation: exits are reconstructed with the option's `*_entry_iv`, not live IV.
- The tracker here runs with all governance/EV caps OFF (`require_ev_authority=False`); if you need EV-authority enforcement you must construct your own `WheelTracker` with those flags and pass tokens.

### Minimal usage example
```python
import sys; sys.path.insert(0, r"C:\Users\merty\Desktop\Day-Trading-Bot\vendor\swe")
import pandas as pd
from datetime import date
from backtests.simulator import WheelBacktester

trade_universe = pd.DataFrame([{
    "date": "2026-01-05", "ticker": "AAPL", "strategy_leg": "short_put",
    "strike": 150.0, "mid_price": 2.50, "implied_vol": 0.25,
    "expiration": "2026-02-20", "dte": 46, "expected_value": 35.0,
}])
ohlcv_data = {"AAPL": pd.DataFrame({
    "date": [date(2026, 1, 5), date(2026, 1, 6)], "close": [155.0, 154.0],
})}

bt = WheelBacktester(initial_capital=100_000, profit_target_pct=0.60,
                     stop_loss_multiple=2.0, max_positions=10, risk_free_rate=0.04)
summary_df = bt.run_backtest(trade_universe, ohlcv_data,
                             start_date="2026-01-05", end_date="2026-01-06")
# summary_df = bt.tracker.get_performance_summary(); bt.trade_log holds the event audit
```

### Non-obvious caller requirements
- `trade_universe` MUST contain the exact columns `date, ticker, strategy_leg, strike, mid_price, implied_vol, expiration, dte` (and optionally `expected_value`); `strategy_leg` values must be literally `'short_put'`/`'covered_call'`.
- `ohlcv_data` keys are tickers; each frame needs `date` (Python `date`) and `close` columns; missing dates silently skip that ticker for the day.
- The simulator does NOT model commissions/slippage itself — supply those via a tracker configured for costs if needed.
- There is no provided "feed an external bar" hook — to do event-driven intraday replay you must subclass and override `run_backtest`/`_check_exits`/`_get_current_prices`.

---

## data/feature_store.py

Module path: `vendor/swe/data/feature_store.py` (importable as `data.feature_store`).
One-line purpose: Parquet-backed, partitioned feature store with point-in-time reads, change-detection writes, lineage tracking, per-column stats, and a JSON registry.

### Import-time behavior (CRITICAL)
- The module itself imports `pandas` (hard requirement) and tries `pyarrow` / `pyarrow.parquet` (soft — sets `PYARROW_AVAILABLE`, falls back to `pandas.to_parquet`/`read_parquet` if missing). No numpy/scipy/statsmodels/arch/sklearn/requests imported by this module.
- BUT: `from data.feature_store import ...` first runs `data/__init__.py`, which is a heavy re-export hub. It eagerly imports `bloomberg`, `bloomberg_import`, `bloomberg_loader`, `consolidated_loader`, `feature_pipeline`, `observability`, `orchestrator`, `pipeline`, `quality`, etc. Those pull in the full Bloomberg data stack and likely numpy/pandas and possibly a Bloomberg/Excel COM layer. To avoid this, import the file directly by path / as a standalone module rather than through the `data` package, or ensure the package's transitive deps are installed.

### Parquet / partition conventions
- Base path default: `data/features` (relative). Layout:
  ```
  <base_path>/<category>/ticker=<TICKER>/
      data.parquet      # the feature rows
      metadata.json     # FeatureMetadata
      stats.json        # list[FeatureStats]
  <base_path>/_lineage/lineage.parquet
  <base_path>/_stats/        (created, reserved)
  <base_path>/_registry/registry.json
  <base_path>/_locks/<name>.lock
  ```
- Partitioning is ONLY by `ticker=<TICKER>` (a Hive-style directory). There is NO `date=` partition directory in this store — date is a COLUMN inside `data.parquet`, not a partition. (The `date=` partition layout the task mentions belongs to the option-tape script below, not to this feature store.)
- Writes are atomic (temp file + `os.replace`), snappy-compressed by default, `index=False`, guarded by a per-`category_ticker` file lock. Registry/lineage writes use named locks (`registry`, `lineage`).
- Point-in-time / PIT: enforced at READ time via the `as_of` param, which filters `df[pd.to_datetime(df["date"]) <= as_of]`. The PIT key column is literally named `date`. There is no separate "as-of"/ingestion-timestamp column persisted in the parquet — provenance/PIT stamping lives in `metadata.json` (see below), not in the data rows.
- Provenance columns are NOT injected into `data.parquet`. Provenance is tracked out-of-band in `FeatureMetadata` (`source_hash`, `source_files`, `created_at`, `updated_at`, `version`, `schema_version`) and in `LineageRecord` rows.

### Enums

`FeatureCategory(StrEnum)` — string-valued; member `.value` equals the lowercase string. Members: `OHLCV="ohlcv"`, `OPTIONS_CHAIN="options_chain"`, `OPTIONS_FLOW="options_flow"`, `EARNINGS="earnings"`, `DIVIDENDS="dividends"`, `FUNDAMENTALS="fundamentals"`, `RATES="rates"`, `TECHNICAL="technical"`, `VOLATILITY="volatility"`, `OPTIONS_FEATURES="options_features"`, `DYNAMICS="dynamics"`, `VOL_EDGE="vol_edge"`, `ASSIGNMENT="assignment"`, `EVENTS="events"`, `REGIME="regime"`, `LABELS="labels"`, `COMPOSITE="composite"`. Anywhere a `category` is accepted you may pass either the enum or a raw string.

### Dataclasses

`FeatureMetadata` — fields (all required unless a default is shown):
- `category: str`
- `ticker: str`
- `created_at: str` (ISO-8601 string)
- `updated_at: str` (ISO-8601 string)
- `row_count: int`
- `date_range: tuple[str, str]` (min date string, max date string, e.g. `("2024-01-01","2024-06-30")`; `("unknown","unknown")` if no `date` column)
- `columns: list[str]`
- `source_hash: str` (16-hex-char sha256 prefix of a ≤1000-row sample — change-detection key)
- `source_files: list[str]` (lineage)
- `computation_time_ms: int`
- `version: int = 1`
- `schema_version: str = "1.0"`
- Methods: `to_dict() -> dict` (via `asdict`); classmethod `from_dict(d: dict) -> FeatureMetadata` (coerces `date_range` back to a tuple).

`FeatureStats` — fields:
- `column: str`, `dtype: str`, `count: int`, `null_count: int`, `null_pct: float`
- Optional numeric (only populated for numeric dtypes): `mean: float|None=None`, `std: float|None=None`, `min: float|None=None`, `max: float|None=None`, `p25: float|None=None`, `p50: float|None=None`, `p75: float|None=None`
- `unique_count: int|None=None` (only for object/categorical dtypes)

`LineageRecord` — fields: `feature_category: str`, `feature_ticker: str`, `source_category: str`, `source_ticker: str`, `source_file: str|None`, `transformation: str`, `timestamp: str`. Method `to_dict() -> dict`.

### Class `FeatureStore`

Constructor: `FeatureStore(base_path: str | Path = "data/features", cache_ttl_hours: int = 24, enable_compression: bool = True)`. Side effects in `__init__`: creates the directory tree and loads/creates `_registry/registry.json`. No external data needed to construct.

Public methods (exact signatures):

- `write_features(category: str | FeatureCategory, ticker: str, df: pd.DataFrame, source_files: list[str] | None = None, source_category: str | None = None, transformation: str = "unknown", force: bool = False) -> FeatureMetadata`
  - Requires `df` to carry a date — either a `date` column, or a `DatetimeIndex` (it is reset and the first column renamed to `date`). If neither, `date_range` becomes `("unknown","unknown")` and PIT reads on that set won't filter.
  - Change detection: if existing `source_hash == new hash` and not `force`, write is skipped and the existing `FeatureMetadata` is returned (no-op). Pass `force=True` to overwrite identical data.
  - Side effects: writes `data.parquet`, `metadata.json`, `stats.json`; appends a `LineageRecord` only if `source_category` is provided (lineage is in-memory until `save_lineage()`); updates registry + in-memory cache.

- `read_features(category: str | FeatureCategory, ticker: str, as_of: str | date | datetime | None = None, columns: list[str] | None = None, use_cache: bool = True) -> pd.DataFrame | None`
  - Returns `None` if the feature set does not exist. `as_of` applies the PIT filter on the `date` column (rows with `date <= as_of`). `columns` selects a subset (pyarrow column-pruned read when available). Cache TTL = `cache_ttl_hours`.

- `get_metadata(category, ticker) -> FeatureMetadata | None`
- `get_stats(category, ticker) -> list[FeatureStats] | None`
- `list_features(category: str | FeatureCategory | None = None) -> list[tuple[str, str]]` — `(category, ticker)` tuples, union of registry + filesystem scan, sorted.
- `get_tickers(category) -> list[str]`
- `get_lineage(category, ticker) -> list[LineageRecord]` — only IN-MEMORY records from the current session.
- `save_lineage() -> None` / `load_lineage() -> list[LineageRecord]` — persist/restore `_lineage/lineage.parquet`.
- `delete_features(category, ticker) -> bool`
- `clear_cache() -> None`
- `get_storage_stats() -> dict` — keys `total_size_bytes`, `total_size_mb`, `file_count`, `feature_count`, `cache_entries`.
- `health_check() -> dict` — keys `healthy: bool`, `issues: list[str]`, `storage: dict`.

Module-level: `get_feature_store(base_path: str = "data/features") -> FeatureStore` — process-wide singleton.

### Gotchas
- `source_hash` is computed from at most the first 1000 rows (`df.head(1000)`), so two large datasets that differ only after row 1000 hash identically and a write may be skipped — use `force=True` when in doubt.
- `as_of`-string is parsed with `pd.to_datetime`; the filter is inclusive (`<=`).
- On Windows, the "file lock" is advisory only (it just touches a `.lock` file — no real exclusion); fsync/crash-safety paths are POSIX-only.
- Lineage is NOT auto-persisted; call `save_lineage()` explicitly.
- `get_lineage` returns only records created in the current process unless you `load_lineage()` first.

### Minimal usage
```python
import pandas as pd
from data.feature_store import FeatureStore, FeatureCategory  # NOTE: triggers heavy data/__init__

store = FeatureStore(base_path="data/features")

df = pd.DataFrame({
    "date": pd.date_range("2024-01-01", periods=3, freq="D"),
    "rv_20": [0.18, 0.19, 0.17],
})
meta = store.write_features(
    category=FeatureCategory.VOLATILITY,   # or "volatility"
    ticker="AAPL",
    df=df,
    source_files=["data/raw/aapl_ohlcv.csv"],
    source_category="ohlcv",
    transformation="rolling_realized_vol_20d",
)

# Point-in-time read: only rows on/before 2024-01-02
pit = store.read_features("volatility", "AAPL", as_of="2024-01-02", columns=["date", "rv_20"])
```

## scripts/pull_theta_option_tape.py

Module path: `vendor/swe/scripts/pull_theta_option_tape.py`. Purpose: CLI puller for intraday option trade + quote tape from a local Theta Terminal; writes partitioned parquet. (Documented for OUTPUT SCHEMA only — do not execute; it opens sockets to `127.0.0.1:25503` and hits the Theta API.)

### Not a library
This is a `main()` CLI script (`argparse`), not an importable API. Importing it runs module-top side effects: it reconfigures stdout/stderr to UTF-8, inserts the repo root on `sys.path`, imports `pandas`, and imports `from engine.theta_connector import ThetaConnector, _normalise_theta_symbol` (which triggers `engine/__init__`'s heavy eager imports — see regime section). Consumers should treat this strictly as a producer of on-disk parquet that the intraday engine then READS.

### Output partition layout
```
data_processed/theta/option_tape/
    ticker=<SYM>/
        date=<YYYY-MM-DD>/        # ISO date (note: dashes, e.g. date=2026-05-16)
            trades.parquet        # trade-by-trade prints
            quotes.parquet        # 1-minute bid/ask bars
```
Both partition keys are Hive-style. `ticker=` is the raw input symbol (the symbol is normalized only for the Theta API call, not for the directory name). Files written with `index=False`. A file is absent for a day-contract that returned no rows.

### trades.parquet — schema
Documented per-file schema (module docstring):
`ts, expiration, strike, right, price, size, exchange, condition, nbbo_bid, nbbo_ask, side_inferred`

Important caveats about what actually lands:
- Columns are passed through from the Theta `/v3/option/history/trade` response, then lowercased (`df.columns = [c.lower() for c in df.columns]`). The exact set depends on Theta's response; the docstring lists the expected/normalized names.
- `ts`: the trade timestamp. The script searches the response for the first of `ts, timestamp, trade_time, created`, parses it with `pd.to_datetime`, drops un-parseable rows, and RENAMES the chosen column to `ts`. So downstream you always read the timestamp column as `ts` (datetime64). No timezone normalization is applied beyond `pd.to_datetime` defaults — treat tz as provider-native.
- `expiration`: option expiry (provider format, typically `YYYYMMDD` as a string/int).
- `strike`: option strike (dollars, provider-native scale).
- `right`: `call`/`put` (provider casing; the ATM helper lowercases when comparing).
- `price`: trade print price (per-share option premium, dollars; NOT ×100).
- `size`: contracts traded.
- `exchange`, `condition`: provider passthrough.
- `nbbo_bid`, `nbbo_ask`: National Best Bid/Offer at/around the print (per-share dollars). These are only present if the Theta trade endpoint returns them; `side_inferred` is only added when `{nbbo_bid, nbbo_ask, price}` are ALL present.
- `side_inferred` ∈ `{"buy", "sell", "mid"}` — buy/sell classification from `_classify_side(row)`:
  - returns `"mid"` if any of bid/ask/price is NaN OR `ask <= bid` (crossed/locked/missing NBBO);
  - `"buy"` if `price >= ask - 1e-9` (print at/above ask = buy-initiated, lifting the offer);
  - `"sell"` if `price <= bid + 1e-9` (print at/below bid = sell-initiated, hitting the bid);
  - otherwise midpoint rule: `"buy"` if `price > mid` else `"sell"`.
  - SIGN/SEMANTIC convention for dealer flow: `"buy"` = customer buy-initiated = dealer SELLS (dealer is short that contract); `"sell"` = customer sell-initiated = dealer BUYS. This is the input the dealer-positioning module uses to back out dealer inventory.

### quotes.parquet — schema
Documented per-file schema:
`ts, expiration, strike, right, bid, ask, bid_size, ask_size, mid`
- Sourced from `/v3/option/history/quote` with `interval="1m"` → 1-minute bars.
- `ts`: chosen from first of `ts, timestamp, bar_start, created`, parsed to datetime64, renamed to `ts`.
- `bid, ask`: per-share dollars; `bid_size, ask_size`: contracts at the top of book.
- `mid`: COMPUTED by the script as `(bid + ask) / 2` (only added when both `bid` and `ask` columns exist). Not from the provider.

### Caller must know
- Both `trades.parquet` and `quotes.parquet` may not both exist for a day-contract. Schema is best-effort over Theta's actual columns; always select defensively. Prices are per-share option premium in dollars — apply your own contract multiplier (×100) for notional. `side_inferred` may be absent if NBBO columns weren't returned.

## engine/regime_detector.py

Module path: `vendor/swe/engine/regime_detector.py`. Purpose: heuristic market-regime classification (volatility regime, trend regime, vol term structure) producing a `RegimeState` with discrete enums + numeric scores. This is the rule-based detector (the HMM in `regime_hmm.py` is its probabilistic replacement).

### Import-time behavior (CRITICAL)
- The module imports `numpy`, `pandas`, and `scipy.stats` (`from scipy import stats`). So scipy + numpy + pandas are required.
- `from engine.regime_detector import ...` triggers `engine/__init__.py`, which eagerly imports the ENTIRE engine surface (ev_engine, wheel_runner, candidate_dossier, dealer_positioning, monte_carlo, option_pricer, performance_metrics, portfolio_tracker, risk_manager, shared_valuation, signal_context, signals, stress_testing, transaction_costs, volatility_surface, wheel_tracker, event_calendar). Expect this to require numpy/scipy/pandas and likely statsmodels/arch/sklearn transitively, and to be slow. To use just the regime detector cheaply, import the file directly rather than via the `engine` package.

### Enums (string-valued via `.value`)
- `VolatilityRegime(Enum)`: `LOW="low"` (IV <15th pct), `NORMAL="normal"` (15–70), `ELEVATED="elevated"` (70–90), `HIGH="high"` (90–97), `CRISIS="crisis"` (>97, ~VIX>35).
- `TrendRegime(Enum)`: `STRONG_UP`, `WEAK_UP`, `NEUTRAL`, `WEAK_DOWN`, `STRONG_DOWN` (values are snake_case strings).
- `VolTermStructure(Enum)`: `STEEP_CONTANGO`, `CONTANGO`, `FLAT`, `BACKWARDATION`, `STEEP_BACKWARDATION`.

### Dataclass `RegimeState`
Fields (all required positionally): `volatility_regime: VolatilityRegime`, `trend_regime: TrendRegime`, `term_structure: VolTermStructure`, `current_iv: float`, `iv_percentile: float` (0–100), `realized_vol: float`, `iv_rv_spread: float` (`current_iv - realized_vol`), `trend_strength: float` (0–100, R-like), `trend_direction: float` (−1..+1), `vol_regime_score: float` (−1 low vol .. +1 high vol), `trend_regime_score: float` (−1 bearish .. +1 bullish), `regime_confidence: float` (0–1).
- UNITS: `current_iv`, `realized_vol`, `iv_rv_spread` are DECIMAL annualized vols (e.g. 0.20 = 20%/yr), NOT percent. Realized vol annualization factor is `sqrt(252)` (daily→annual). `iv_percentile` is 0–100. `trend_direction`/scores are dimensionless in [−1,1].
- Properties: `is_favorable_for_selling -> bool`; `position_size_multiplier -> float` (0.0–~1.5, product of a vol-regime map × trend-regime map).

### Class `RegimeDetector`
Constructor: `RegimeDetector(vol_lookback: int = 252, trend_lookback: int = 20, rv_window: int = 20)`. All in trading days. Holds mutable internal history (`iv_history`, `regime_history`) — the detector is STATEFUL; reusing one instance across calls feeds confidence/percentile logic.

Primary call:
`detect_regime(current_iv: float, prices: pd.Series, iv_history: pd.Series | None = None, front_iv: float | None = None, back_iv: float | None = None) -> RegimeState`
- `current_iv`: decimal annualized IV (e.g. 0.22).
- `prices`: a price `pd.Series` (close prices). For intraday use, pass your intraday close bars; realized vol is computed from `pct_change()` of this series annualized by `sqrt(252)` — so the annualization assumes the returns are DAILY. If you feed intraday (e.g. 1-min) bars, the `sqrt(252)` factor will NOT correctly annualize; supply daily closes or pre-aggregate, or treat `realized_vol` as a relative number only.
- `iv_history`: optional decimal-IV series for percentile ranking (needs >30 points; else falls back to internal history, else absolute thresholds).
- `front_iv`, `back_iv`: decimal IVs for the front/back expiries; term structure from ratio `front_iv/back_iv` (`<0.85` steep contango, `<0.97` contango, `<1.03` flat, `<1.15` backwardation, else steep backwardation). If either is `None` → `FLAT`.

Other public method:
`get_strategy_adjustments(regime: RegimeState) -> dict[str, any]` — returns dict with `position_size_mult`, `delta_target`, `dte_preference`, `profit_target_mult`, `stop_loss_mult`, `new_positions_allowed`, `reason` (list of strings).

Module-level function:
`calculate_regime_signals(prices: pd.DataFrame, iv_column: str = "iv", close_column: str = "close") -> pd.DataFrame` — walks the DataFrame row-by-row (expanding window, PIT-correct: uses `.iloc[:i+1]`), returns a DataFrame indexed by the original index with columns `vol_regime`, `trend_regime`, `iv_percentile`, `trend_strength`, `trend_direction`, `position_mult`, `favorable_for_selling`. If `iv_column` is absent, `current_iv` defaults to 0.20.

### How to get a single regime LABEL for a symbol from intraday bars
```python
import pandas as pd
from engine.regime_detector import RegimeDetector  # NOTE: triggers heavy engine/__init__

prices = pd.Series([100.0, 101.2, 100.8, 102.1, 103.0])  # close bars for the symbol
det = RegimeDetector()
state = det.detect_regime(current_iv=0.22, prices=prices, front_iv=0.21, back_iv=0.24)
vol_label   = state.volatility_regime.value   # e.g. "normal"
trend_label = state.trend_regime.value        # e.g. "weak_up"
size_mult   = state.position_size_multiplier  # float, ~0.0..1.5
```
Gotchas: `detect_regime` needs ≥2 returns or `realized_vol` defaults to 0.20; needs ≥`trend_lookback` prices or trend is `NEUTRAL`/0. Percentile ranking requires ≥30 IV-history points to be meaningful, otherwise it uses fixed absolute IV thresholds. The detector accumulates state — construct a fresh `RegimeDetector` per independent symbol if you want isolated histories.

## engine/regime_hmm.py

Module path: `vendor/swe/engine/regime_hmm.py`. Purpose: pure-numpy 4-state Gaussian Hidden Markov Model regime detector (Baum-Welch EM fit, filtered posterior, Viterbi decode) fit to daily log-returns (and optionally realized-vol features); the probabilistic replacement for the heuristic detector.

### Import-time behavior
- Module imports only `numpy` and `scipy.stats.norm`. Deliberately does NOT depend on `hmmlearn`. But `from engine.regime_hmm import GaussianHMM` triggers the same heavy `engine/__init__.py` eager-import cascade described above. Import the file directly to avoid it.

### Dataclass `HMMFit`
Fields: `n_states: int`, `n_features: int`, `start_prob: np.ndarray` shape `(K,)`, `trans_mat: np.ndarray` shape `(K, K)`, `means: np.ndarray` shape `(K, D)`, `stds: np.ndarray` shape `(K, D)`, `log_likelihood: float`, `n_iter: int`, `converged: bool`, `state_labels: list[str] = field(default_factory=list)`.

### State ordering / labels (sign convention)
After fitting, states are sorted ascending by a return-adjusted score `means[:,0] - 0.5*stds[:,0]`, so index 0 = WORST, index K−1 = BEST. For K=4 labels are `["crisis", "bear", "normal", "bull_quiet"]` (index 0..3). For K=3: `["crisis","normal","bull"]`; K=2: `["bear","bull"]`; else `state_i`. `bull_quiet` (highest index) is the best regime for the wheel strategy.

### Class `GaussianHMM`
Constructor: `GaussianHMM(n_states: int = 4, n_iter: int = 50, tol: float = 1e-3, random_state: int | None = 42)`.

Public methods:
- `fit(observations: np.ndarray) -> HMMFit`
  - `observations`: 1-D array (T,) treated as univariate returns → reshaped to (T,1); or 2-D (T, D) where each column is a feature. Column 0 is the PRIMARY feature used for seeding/labeling — pass DAILY LOG-RETURNS in column 0 (not prices, not percent; e.g. ~1e-2 scale). Optional column 1 = rolling realized vol.
  - Requires `T >= n_states*3` (else `ValueError`). Refuses degenerate input: raises `ValueError` if `std(obs[:,0]) < 1e-7` (constant returns). Callers should treat a fit failure as the neutral 1.0 multiplier.
  - Diagonal-covariance only (features assumed independent within a state).
- `predict_proba(observations: np.ndarray) -> np.ndarray` — returns the FILTERED posterior `P(state_t | obs_1..t)`, shape `(T, K)`, rows sum to 1. (Filtered, not smoothed — uses forward `alpha` only.) The current regime is the last row.
- `viterbi(observations: np.ndarray) -> np.ndarray` — most-likely hard state path, shape `(T,)`, int state indices (in the sorted 0=worst..K−1=best convention).
- `position_multiplier(state_probs: np.ndarray) -> float` — maps a 1-D posterior (one row of `predict_proba`, length K) to a position-size multiplier in [0, 1.25] via per-label weights `{crisis:0.2, bear:0.5, normal:1.0, bull_quiet:1.25}` (unknown labels weight 1.0). This feeds the EV engine's `regime_multiplier` field directly. Cold regimes pull <1, hot regime up to ~1.25.
- All three raise `RuntimeError("HMM not fit yet")` / `"HMM not fit"` if `fit_result is None`.

### Units / gotchas
- Inputs are DAILY LOG-RETURNS (dimensionless, ~1e-2 magnitude), NOT prices and NOT percent. For intraday use you must convert intraday bars to a returns feature; the model itself is scale-agnostic but the degenerate-std guard (1e-7) and the label heuristic assume return-like magnitudes.
- `random_state=42` default makes fits reproducible.
- No annualization is performed; means/stds are in the same units as the input column (per-bar).

### How to get a regime label/enum + current state from bars
```python
import numpy as np
from engine.regime_hmm import GaussianHMM  # NOTE: triggers heavy engine/__init__

log_returns = np.diff(np.log(close_prices))   # daily log-returns, shape (T,)
hmm = GaussianHMM(n_states=4, random_state=42)
hmm.fit(log_returns)                            # raises if T<12 or returns ~constant
probs = hmm.predict_proba(log_returns)          # (T, 4), filtered posterior
current_idx = int(np.argmax(probs[-1]))         # 0=crisis .. 3=bull_quiet
current_label = hmm.fit_result.state_labels[current_idx]   # e.g. "normal"
size_mult = hmm.position_multiplier(probs[-1])  # float in [0, 1.25]
```

### Choosing between the two regime modules
`regime_detector.RegimeDetector` returns rich discrete enums (`VolatilityRegime`/`TrendRegime`/`VolTermStructure`) plus IV/RV diagnostics from a price series + IV — use it when you have IV data and want named labels. `regime_hmm.GaussianHMM` returns a probabilistic posterior + a sorted-by-quality label (`crisis/bear/normal/bull_quiet`) from returns alone — use it when you only have price/return bars and want uncertainty-aware sizing. Both share the heavy `engine/__init__` import cost.

---
