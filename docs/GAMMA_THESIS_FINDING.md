# Does the dealer-gamma regime condition realized behavior? (real-data test)

_2026-06-23. The engine's reason to exist is the **gamma-spine premise**: that the
dealer-gamma regime conditions the *character* of the tape — positive net GEX
(dealers long gamma) → vol-dampening / mean-reverting; negative net GEX (short
gamma) → vol-amplifying / trending. This had never been tested on real options
data at scale. It is now._

## Method

- **Real GEX series.** Reconstructed daily dealer-GEX for SPY/QQQ from the deep
  `theta/index_reference/option_history` (per-contract daily NBBO + OI, 2018→2026)
  via `intraday/data/index_chain_history.py`: parity spot from nearest-forward C/P
  mids, IV Black-Scholes-inverted at the GEX consumer's tenor, real prior-session
  3m risk-free rate, known dividend yield. Front-monthly tenor (~30 DTE).
  Network-free; PIT throughout. (The reconstructed parity spot matches independent
  Yahoo closes to **<0.3%**, and a known-IV→price→reconstruct→recover-IV round-trip
  is exact — see `tests/test_index_chain_history.py`.)
- **Outcome.** Next-day absolute log return `|ret_{t+1}|`, computed from the
  reconstructed parity spot (which is itself a real EOD close). Regime at the close
  of day `t` (GEX sign) conditions day `t+1`'s move — strictly PIT.
- **The skeptical control.** Short-gamma regimes *are* high-vol regimes, and vol
  clusters — so a naive "short-gamma → bigger next move" could be pure vol
  clustering. We therefore control for the recent realized-vol level:
  multivariate OLS `|ret_{t+1}| ~ short_gamma + recent_vol`, and a within-vol-tercile
  Mann-Whitney. The thesis is only credited if the gamma signal **survives** the
  control. (`scripts/build_real_gex_series.py`, `scripts/analyze_gex_thesis.py`.)

## Result

| Sample | n (fwd pairs) | regime balance | naive ratio (short/long) | naive p | **OLS short_gamma, controlling recent_vol** | within-tercile | survives? |
|---|---:|---|---:|---:|---|---|---|
| **SPY 2021–2026** | 1020 | 26% long-gamma | **1.89×** (85 vs 45 bps) | <1e-4 | **+26.6 bps, t=4.95, p<0.001** | all 3 terciles p<0.001 | **YES** |
| **QQQ 2021–2026** | 1020 | 49% long-gamma | **1.60×** (124 vs 77 bps) | <1e-4 | **+27.3 bps, t=4.44, p<0.001** | mid+high p≤0.004 | **YES** |
| SPY 2025–2026 (8 mo) | 139 | 18% long-gamma (thin) | 1.44× (68 vs 47 bps) | 0.034 | +17.1 bps, t=1.39, p=0.166 | — | underpowered |

On **both powered 5-year samples — two independent symbols — the dealer-gamma
regime independently predicts** the next day's realized move *after* controlling
for vol clustering, with a strikingly consistent coefficient (**+26.6 bps on SPY,
+27.3 bps on QQQ**, both p<0.001). It holds *within* fixed vol terciles (all three
on SPY; mid+high on QQQ) — i.e. even at a fixed vol level, short-gamma days precede
larger next-day moves. The 8-month SPY sample only *looked* weakened because it had
just 25 long-gamma days; with real power the effect is robust and **replicates
across SPY and QQQ**.

## Honest interpretation

1. **The premise is real, and it replicates.** The gamma regime conditions
   realized volatility, and the relationship is NOT merely vol clustering — it
   survives the obvious confound on two independent 5-year, 1000+-observation
   samples (SPY +26.6 bps, QQQ +27.3 bps, both p<0.001), and holds inside fixed-vol
   buckets. The near-identical coefficient across two symbols is strong evidence
   this is a real structural effect, not a fluke. Genuine real-data support for the
   engine's central idea.
2. **A real signal is not a tradeable edge.** Knowing short-gamma precedes bigger
   moves does not mean the strategies that try to *monetize* it make money. The
   published headline stands: S1/S3/S4/S5 are net-negative after costs on real
   data. The precise, actionable conclusion is therefore: **the signal is real; the
   problem is execution and cost, not the thesis.** Effort is better spent on
   cost/structure (e.g. expressing the regime via cheaper instruments or longer
   holds) than on doubting the premise.
3. **Caveats.** Regime is the coarse GEX *sign* (not magnitude/flip-distance);
   chains are *reconstructed* (BS-inverted from NBBO mids), not a third-party GEX
   feed; returns use the parity spot (validated to <0.3% vs Yahoo); a single ~30-DTE
   tenor; and the SWE dealer analyzer bakes in a `LONG_CALLS_SHORT_PUTS` dealer
   assumption. These would all be worth varying before betting real money on the
   effect.

## Reproduce

```bash
python scripts/build_real_gex_series.py --symbol QQQ --start 2021-06-01 --end 2026-06-10
python scripts/analyze_gex_thesis.py --symbols QQQ SPY
```
