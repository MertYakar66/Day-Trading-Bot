# Can the validated gamma-spine signal be TRADED? (real-NBBO monetization test)

_2026-06-23. Follow-on to `docs/GAMMA_THESIS_FINDING.md`. Adversarially verified
(5 independent skeptics + synthesis): conclusion stands; no look-ahead; gross P&L
math confirmed; this write-up incorporates every required correction._

The gamma-spine **signal** is established on real data: the dealer-gamma regime
conditions the next day's realized move (short-gamma → bigger, **~+27 bps, p<0.001**,
SPY *and* QQQ, vol-controlled). The published headline is nonetheless **no tradeable
edge**. The prior report framed that as _"real signal, but loses to costs — the
problem is execution/cost, not the thesis."_ That sentence was an **assertion**. This
test measures it — with **real daily NBBO bid/ask** — and the measurement **sharpens,
and partly corrects, the claim**.

## Method

For every day `t` in the validated GEX series (SPY + QQQ, 2021-06 → 2026-04, ~1047
days each), buy the **real ATM straddle** at the close of `t` and unwind it at the
next session — pricing the trade three ways:

| Layer | What it asks | How |
|---|---|---|
| **(A) Mispricing** | Is the extra short-gamma move already in the option's *implied* move? | realized `|ret_{t+1}|` vs `ATM_IV·√(1/252)`, by regime |
| **(B) Gross P&L** | Does conditioning on the regime have alpha *before costs*? | straddle **mid → mid** |
| **(C) Net P&L** | Does anything survive the *real* spread? | buy **ask**, sell **bid** |

Regime = sign of dealer-GEX from the validated series (short-gamma = GEX<0). The ATM
call+put NBBO is read straight from the deep history (`theta/index_reference/
option_history`) by the new, round-trip-tested `intraday/data/straddle_history.py`.
PIT throughout (regime + strike from data known at the close of `t`; exit values the
**same** contract at `t+1` — verified 1047/1047 a strictly-next-session hold). Network-free.
CIs are **moving-block bootstrap** (block=10), which preserves the serial correlation
that the i.i.d. t-test ignores.

## Result (pooled SPY+QQQ, 2093 round trips; per-symbol replicates)

| Measure | Short-gamma | Long-gamma | Read |
|---|---:|---:|---|
| **(B) GROSS straddle return / trade** | **−0.15%** (t=−1.0; boot-CI **[−41, +15] bps**) | −0.16% | **~zero — no gross alpha** |
| **(B) GROSS regime gap (SG−LG)** | **+0.5 bps**, boot-CI **[−48, +51] bps** | | **straddles 0 — regime adds nothing** |
| **(C) NET straddle return / trade** | **−1.22%** (t=−8.5) | −1.33% | real spread (~1.07% round trip) bleeds it |
| (A) realized/implied, **fair-adjusted** | 0.95× (raw 0.76×) | 0.78× (raw 0.62×) | short-gamma **~fairly priced** |
| Short straddle on long-gamma, NET | — | −1.06% (t=−4.8) | selling vol also loses net |

Per symbol the gross result replicates: SPY short-gamma gross −0.09% (t=−0.45), QQQ
−0.24% (t=−1.10) — both indistinguishable from zero, regime gap insignificant in each.

## Honest interpretation

1. **The signal is real but FULLY PRICED into the premium — there is no gross alpha.**
   Buying the validated bigger move as a straddle returns ≈0 *before any cost*
   (−0.15%/trade), and conditioning on the regime adds nothing: the GROSS short-minus-
   long-gamma return gap is **+0.5 bps with a bootstrap CI straddling zero**. The larger
   short-gamma move is offset by the higher premium (IV) you pay for it. Layer (A)
   confirms it directly: once the half-normal factor is removed (see §3), short-gamma
   realizes **0.95× of its implied move — essentially fair**. The bigger move *is* the
   higher IV.

2. **This refines — and partly corrects — the prior "killed by costs" framing.** The
   problem is **not primarily cost**: there is no gross edge to begin with. The sharper,
   honest statement is **"priced in first, then costs."** Net of the real (tight, ~1%
   round-trip) spread every variant bleeds hard (−1.2%/trade, t<−8) — but cost is the
   *second* nail, not the first. A rigorous monetization test disciplined an
   over-optimistic interpretation; that is the test doing its job.

3. **The (A) mispricing layer needs a half-normal caveat (and gets one).** For a
   near-normal return, `E|ret| = σ·√(2/π) ≈ 0.80·σ`, so a realized/IV-implied ratio of
   ~0.80 is exactly what **fair** pricing produces — the raw 0.76× is **not** evidence
   of over-pricing. Dividing by 0.798 gives the fair-adjusted realized-vs-implied
   **vol**: short-gamma 0.95× (≈fair), long-gamma 0.78× (modestly rich). Only the
   *regime contrast* (p=0.0024, computed on the additive gap where the factor cancels)
   is informative, and it is modest and — per (B) — not tradeable. The authoritative
   pricing test is the straddle **gross P&L (B)**, which prices the real `|S_T−K|`
   payoff; (A) is descriptive color.

4. **On statistical power — stated honestly.** The bare gross-vs-zero test is
   *underpowered* to call −0.15% "exactly zero" (MDE ≈ 41 bps at 80% power; gross
   boot-CI [−41, +15] bps). So "priced in" does **not** rest on that single test. It
   rests on the convergence of three things: (i) the gross regime gap is ~0 with a CI
   straddling zero (conditioning buys nothing), (ii) fair-adjusted (A) shows short-gamma
   realizing ≈ its implied move, and (iii) the net result is decisively negative
   (t=−8.5, robust to any plausible serial-correlation inflation).

5. **Reconstruction / bias disclosures (all reinforce no-edge).** (a) The ATM straddle
   is the nearest *listed* strike — measured <0.06% off-spot on average (max 0.54%), an
   unbiased symmetric approximation. (b) **mid→mid gross is the optimistic (zero-cost)
   bound and ask→bid net the pessimistic (full-spread) bound** — the *optimistic* bound
   already shows no edge, so discreteness and costs only reinforce it. (c) The two-sided
   spread filter is non-binding at the ATM (0/1047 entry refusals; ATM spreads ~0.5%),
   so no liquidity survivorship. (d) The parity spot feeds **only** the (A) realized-move
   metric, not the (B) gross-P&L test (which uses `straddle_mid = C+P`, orthogonal to
   parity's `C−P`) — so there is **no circularity** in the gross-alpha conclusion. (e)
   American/dividend effects over a 1-day hold are <0.005%/day of spot and symmetric
   across entry/exit.

6. **Scope.** This tests **one** expression — a 1-day-hold ATM straddle at ~30 DTE, EOD
   fills, coarse GEX *sign* (not magnitude/flip-distance), on *reconstructed* NBBO. It
   does not rule out that the regime is monetizable through a *different, cheaper*
   structure (longer holds, calendar/diagonal, delta-hedged gamma, or index futures). It
   rules out the most direct expression and shifts the burden of proof: the regime's
   realized-vol effect is real, but harvesting it requires beating a premium that already
   embeds it.

## Reproduce

```bash
# requires the validated GEX series (build_real_gex_series.py) under data_raw/realdata_validation/
python scripts/test_gamma_monetization.py --symbols SPY QQQ
python -m pytest tests/test_straddle_history.py -q
```
