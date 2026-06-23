# Gamma-spine research: honest conclusion & stopping point

_2026-06-23. Capstone over the real-data gamma-spine investigation. Ties together
`docs/GAMMA_THESIS_FINDING.md` (the signal is real) and
`docs/GAMMA_MONETIZATION_FINDING.md` (the signal is priced in)._

## The arc, in one place

| Question | Answer | Evidence |
|---|---|---|
| Is the engine's premise real? Does the **dealer-gamma regime condition realized vol**? | **Yes, and it replicates.** Short-gamma → larger next-day move, ~+27 bps, p<0.001 on SPY *and* QQQ, **after** controlling for vol clustering, inside fixed-vol buckets. | 2 independent 5-yr samples (1000+ fwd pairs each); reconstructed PIT GEX from the deep option history. `GAMMA_THESIS_FINDING.md` |
| Can that signal be **traded**? | **No — it is already priced into the option premium.** A real-NBBO ATM straddle conditioned on the regime has **no gross alpha** (gross ~0, regime-independent; gross SG−LG gap +0.5 bps, bootstrap CI straddling 0). The bigger move *is* the higher IV. | 2093 real round trips, SPY+QQQ, 1-day hold, PIT; adversarially verified (5 skeptics + synthesis). `GAMMA_MONETIZATION_FINDING.md` |
| So what *is* the obstacle? | **Not execution cost — the signal carries no gross edge in the direct expression.** Costs (~1% round-trip spread) then make a no-edge trade strictly losing (net −1.2%/trade, t<−8). | Gross-vs-net decomposition with real bid/ask. |

**Bottom line:** the gamma-spine premise is **genuine but fully priced**. The honest
one-liner is **"priced in first, then costs"** — which supersedes the earlier, too-
optimistic "real signal, killed by costs."

## Why we are stopping here (and not chasing more structures)

The gross straddle return being ~0 is strong, direct evidence that the most natural
alternatives — calendar/diagonal spreads, delta-hedged gamma (which the straddle P&L
already approximates), or a finer GEX signal (magnitude/flip-distance) — are far more
likely to surface **noise or overfit** than a real net edge on the data we can reach.
Pursuing them would risk manufacturing an edge, which this project is explicitly
disciplined against. We are therefore **pausing synthetic-data experiments on this
thesis**, having taken it to the informative limit of the available data.

## The one thing that would actually unlock progress

Every route to a *tradeable* spine runs through the same missing ingredient:

> **Real intraday option tape (order-flow / OFI and intraday NBBO) for SPX/SPY/QQQ.**

Without it:
- **S1** (the spine strategy) stands aside — there is no real intraday OFI to act on.
- The fabricated `win_prob = p_fair + 0.10` in S1/S3/S4/S5 cannot be replaced with a
  *measured* hit-rate.
- The validated regime can be **measured** (as here) but not **traded** intraday.

This is a **data-acquisition decision**, not an analysis task — it requires a scoped
Theta pull the operator runs (this research line is network-free and never touches a
live socket; see the `no-theta-this-session` guardrail). Until that data exists,
further modelling on EOD/reconstructed data has diminishing returns.

## What this investigation leaves behind (all merged to `main`)

- **`intraday/data/index_chain_history.py`** — PIT GEX reconstruction from the deep
  per-contract daily history (BS IV inversion + parity spot), round-trip-tested.
- **`intraday/data/straddle_history.py`** — real-NBBO ATM straddle quotes, round-trip-tested.
- **`intraday/data/swe_offline.py` + `daily_context.py`** — the offline real-data bridge
  (risk-free curve, vol regime) wired into every `FeatureRow` via `--daily-context`.
- Research harnesses under `scripts/` (`build_real_gex_series`, `analyze_gex_thesis`,
  `test_gamma_monetization`) and findings under `docs/`.
- Suite **643 green**, ruff + mypy clean. The honest NO-EDGE headline stands — now
  better understood than at any prior point.
