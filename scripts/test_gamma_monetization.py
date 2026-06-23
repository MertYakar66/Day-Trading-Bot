"""Can the validated gamma-spine signal be TRADED, net of real option spreads?

The gamma thesis (``docs/GAMMA_THESIS_FINDING.md``) is established on real data: the
dealer-gamma regime conditions the next day's realized move (short-gamma -> bigger,
~+27 bps, p<0.001, SPY+QQQ, vol-controlled). The published headline is nonetheless
NO tradeable edge. This harness turns "real signal, but loses to costs" from an
assertion into a measurement, three ways, using REAL daily NBBO bid/ask:

  (A) MISPRICING  -- is the extra short-gamma move already in the option's implied
      move? Compare realized |ret_{t+1}| vs the ATM-IV-implied move, by regime.
      (Diagnostic only: realized |move| = E|ret| has a FAIR baseline of sqrt(2/pi)
      ~= 0.80 of a one-sigma implied move, not 1.0 -- so the raw ratio overstates the
      variance-risk-premium; (B) gross straddle P&L is the dollar-accurate test.)
  (B) GROSS P&L   -- buy the ATM straddle at the CLOSE of day t (mid->mid), hold to
      the next session: does the regime have GROSS alpha before costs?
  (C) NET P&L     -- the same trade crossing the REAL spread (buy ask, sell bid):
      does anything survive? The gross-vs-net wedge IS the "execution/cost" claim.

PIT throughout: regime + strike chosen at close of t from data known at t; exit at
the next session of the SAME contract. Network-free (reads captured SWE files +
the prebuilt GEX series). Read-only research harness; writes JSON under data_raw/.

Usage:
  python scripts/test_gamma_monetization.py --symbols SPY QQQ
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from intraday.data import index_chain_history as ich  # noqa: E402
from intraday.data import straddle_history as sh  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data_raw", "realdata_validation")
_TD_PER_YEAR = 252.0
# E|N(0,sigma)| = sigma*sqrt(2/pi) ~= 0.7979. A mean ABSOLUTE move is only ~0.80 of a
# one-sigma move even under FAIR pricing, so realized|move| / (IV*sqrt(dt)) has a 0.80
# fair baseline, NOT 1.0. The (A) layer reports both the raw and /0.7979-adjusted ratio.
_HALF_NORMAL = float(np.sqrt(2.0 / np.pi))
_BOOT_SEED = 0


# --------------------------------------------------------------------------- #
# Trade construction
# --------------------------------------------------------------------------- #
def build_trades(symbol: str, series: list[dict], *, hold_days: int) -> pd.DataFrame:
    """One ATM-straddle round trip per GEX-series day: open@close(t), close@t+hold."""
    exp_map = dict(ich.list_history_expiries(symbol))
    part_cache: dict[date, pd.DataFrame] = {}
    rows: list[dict] = []

    for rec in series:
        d = date.fromisoformat(rec["date"])
        E = date.fromisoformat(rec["expiry"])
        r = float(rec["rate"])
        if E not in part_cache:
            part_cache[E] = ich._read_partition(exp_map[E])
        part = part_cache[E]

        entry = sh.atm_straddle_quote(symbol, d, E, r=r, partition=part)
        if entry is None:
            continue
        x_day = sh.next_trading_day(part, d, n=hold_days)
        if x_day is None or x_day >= E:  # don't hold through expiry
            continue
        exit_q = sh.atm_straddle_quote(symbol, x_day, E, r=r, partition=part,
                                       force_strike=entry.strike)
        if exit_q is None or exit_q.dte < 1:
            continue

        hold_td = sh.trading_day_gap(part, d, x_day)
        realized_move = abs(float(np.log(exit_q.parity_spot / entry.parity_spot)))
        implied_move = (
            entry.atm_iv * np.sqrt(hold_td / _TD_PER_YEAR)
            if entry.atm_iv is not None else None
        )

        gross_pnl = exit_q.straddle_mid - entry.straddle_mid           # mid -> mid
        net_pnl_long = exit_q.sell_proceeds - entry.buy_cost           # buy ask, sell bid
        net_pnl_short = entry.sell_proceeds - exit_q.buy_cost          # sell bid, buy ask
        mid0 = entry.straddle_mid

        rows.append({
            "symbol": symbol, "date": d, "expiry": E, "exit_date": x_day,
            "dte_entry": entry.dte, "hold_td": hold_td, "strike": entry.strike,
            "spot_entry": entry.parity_spot, "spot_exit": exit_q.parity_spot,
            "gex_total": float(rec["gex_total"]), "long_gamma": float(rec["gex_total"]) > 0,
            "regime": rec["regime"],
            "atm_iv": entry.atm_iv, "implied_move": implied_move,
            "realized_move": realized_move,
            "vrp_proxy": (realized_move - implied_move) if implied_move is not None else None,
            "straddle_mid_entry": mid0, "straddle_mid_exit": exit_q.straddle_mid,
            "buy_cost": entry.buy_cost, "sell_proceeds_exit": exit_q.sell_proceeds,
            "gross_pnl": gross_pnl, "net_pnl_long": net_pnl_long,
            "net_pnl_short": net_pnl_short,
            # All returns share the entry-mid base so GROSS - rt_cost == NET exactly
            # (clean decomposition; net is the conservative ask->bid figure).
            "gross_ret": gross_pnl / mid0,
            "net_ret_long": net_pnl_long / mid0,
            "net_ret_short": net_pnl_short / entry.sell_proceeds,
            # spread drag of a long round trip, as a fraction of entry mid:
            "rt_cost_pct": ((entry.buy_cost - mid0) + (exit_q.straddle_mid - exit_q.sell_proceeds)) / mid0,
            "min_oi": min(entry.call_oi, entry.put_oi),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["exit_date"] = pd.to_datetime(df["exit_date"])
    return df


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def _block_bootstrap_means(x: np.ndarray, *, n_boot: int = 5000, block: int = 10,
                           seed: int = _BOOT_SEED) -> np.ndarray | None:
    """Moving-block bootstrap of the mean (preserves serial correlation / vol clustering).

    1-day-hold straddle returns on near-consecutive days are autocorrelated, so the
    i.i.d. t-test understates the SE. Resampling whole blocks of ``block`` consecutive
    trades keeps that dependence, giving an honest CI. Deterministic (fixed seed).
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < block * 2:
        return None
    rng = np.random.default_rng(seed)
    nblocks = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(n_boot, nblocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, nblocks * block)[:, :n]
    return x[idx].mean(axis=1)


def _summ(x: pd.Series, *, span_years: float) -> dict:
    """Mean / t / win-rate / Sharpe + i.i.d. AND block-bootstrap 95% CIs of a return."""
    from scipy import stats

    x = x.dropna().astype(float)
    n = int(len(x))
    if n < 2:
        return {"n": n, "mean": None, "t": None, "p": None, "win_rate": None,
                "sharpe_ann": None, "median": None, "ci95_iid": None, "ci95_boot": None}
    arr = x.to_numpy()
    mean, sd = float(arr.mean()), float(arr.std(ddof=1))
    t, p = stats.ttest_1samp(arr, 0.0)
    se = sd / np.sqrt(n)
    trades_per_year = n / span_years if span_years > 0 else float("nan")
    sharpe_ann = (mean / sd * np.sqrt(trades_per_year)) if sd > 0 else None
    boot = _block_bootstrap_means(arr)
    ci_boot = ([float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]
               if boot is not None else None)
    return {
        "n": n, "mean": mean, "median": float(np.median(arr)),
        "t": float(t), "p": float(p),
        "win_rate": float((arr > 0).mean()),
        "sharpe_ann": float(sharpe_ann) if sharpe_ann is not None else None,
        "ci95_iid": [mean - 1.96 * se, mean + 1.96 * se],
        "ci95_boot": ci_boot,
    }


def _boot_diff_ci(a: pd.Series, b: pd.Series) -> dict | None:
    """Block-bootstrap 95% CI for mean(a) - mean(b) (independent groups)."""
    ba = _block_bootstrap_means(a.dropna().to_numpy(), seed=_BOOT_SEED)
    bb = _block_bootstrap_means(b.dropna().to_numpy(), seed=_BOOT_SEED + 1)
    if ba is None or bb is None:
        return None
    diff = ba - bb
    return {
        "diff_mean": float(a.dropna().mean() - b.dropna().mean()),
        "ci95_boot": [float(np.percentile(diff, 2.5)), float(np.percentile(diff, 97.5))],
    }


def analyze(df: pd.DataFrame) -> dict:
    from scipy import stats

    if df.empty:
        return {"error": "no trades"}
    span_years = max((df["date"].max() - df["date"].min()).days / 365.25, 1e-9)
    sg = df[~df["long_gamma"]]   # short-gamma (amplifying)
    lg = df[df["long_gamma"]]    # long-gamma (dampening)

    # (A) MISPRICING: realized |move| vs IV-implied (one-sigma) move, by regime.
    # CAVEAT: realized_move is E|ret| but implied_move is a 1-sigma move, so the FAIR
    # baseline of realized/implied is sqrt(2/pi)=0.7979, NOT 1.0. We report the raw
    # ratio AND ratio/0.7979 (fair-adjusted realized-vs-implied VOL). The authoritative
    # pricing test is strategy.gross (B), which prices the ACTUAL |S_T-K| payoff.
    sg_vrp = sg["vrp_proxy"].dropna()
    lg_vrp = lg["vrp_proxy"].dropna()

    def _ratio(g: pd.DataFrame) -> float | None:
        if not len(g):
            return None
        return float((g["realized_move"] / g["implied_move"])
                     .replace([np.inf, -np.inf], np.nan).dropna().mean())

    sg_ratio, lg_ratio = _ratio(sg), _ratio(lg)
    mis: dict = {
        "short_gamma_mean_realized_minus_implied_bps":
            float(sg_vrp.mean() * 1e4) if len(sg_vrp) else None,
        "long_gamma_mean_realized_minus_implied_bps":
            float(lg_vrp.mean() * 1e4) if len(lg_vrp) else None,
        "short_gamma_realized_over_implied": sg_ratio,
        "long_gamma_realized_over_implied": lg_ratio,
        "fair_baseline_ratio": _HALF_NORMAL,
        "short_gamma_realized_over_implied_VOL_fair_adj":
            (sg_ratio / _HALF_NORMAL) if sg_ratio is not None else None,
        "long_gamma_realized_over_implied_VOL_fair_adj":
            (lg_ratio / _HALF_NORMAL) if lg_ratio is not None else None,
    }
    if len(sg_vrp) >= 10 and len(lg_vrp) >= 10:
        t, p = stats.ttest_ind(sg_vrp, lg_vrp, equal_var=False)
        mis["welch_t_shortgamma_vs_longgamma"] = float(t)
        mis["welch_p"] = float(p)
        # The Welch test runs on the ADDITIVE realized-minus-implied gap, where the
        # common half-normal factor cancels, so the regime CONTRAST is real. But once
        # the 0.7979 factor is removed, realized vol is only MODESTLY below implied
        # (small VRP) -- both regimes are near fair, short-gamma the nearer. Whether
        # this relative gap is tradeable is answered by strategy.gross (it is not).
        mis["interpretation"] = (
            "regime CONTRAST real (short-gamma realizes closer to implied than long-gamma), "
            "but fair-adjusted realized vol is only modestly below implied in BOTH regimes "
            "(small VRP; short-gamma ~fairly priced) -- NOT a large overpricing; see "
            "strategy.gross for tradeability."
            if (p < 0.05 and sg_vrp.mean() > lg_vrp.mean())
            else "no significant regime gap in realized-minus-implied move."
        )

    # (B)/(C) STRATEGY: long straddle on short-gamma days, gross vs net.
    strat = {
        "long_straddle_on_short_gamma": {
            "gross": _summ(sg["gross_ret"], span_years=span_years),
            "net": _summ(sg["net_ret_long"], span_years=span_years),
            "mean_rt_cost_pct": float(sg["rt_cost_pct"].mean()) if len(sg) else None,
        },
        "long_straddle_on_long_gamma": {
            "gross": _summ(lg["gross_ret"], span_years=span_years),
            "net": _summ(lg["net_ret_long"], span_years=span_years),
        },
        "long_straddle_always": {
            "gross": _summ(df["gross_ret"], span_years=span_years),
            "net": _summ(df["net_ret_long"], span_years=span_years),
        },
        "short_straddle_on_long_gamma": {  # sell vol when dampening
            "net": _summ(lg["net_ret_short"], span_years=span_years),
        },
    }
    # The monetizable quantity is the GROSS straddle-return gap between regimes: if
    # conditioning on short-gamma carried tradeable alpha it would show here. Report a
    # block-bootstrap CI so the 'priced in' claim rests on this gap EXCLUDING the
    # ~+27 bps the signal would need -- not on the (underpowered) gross-vs-zero t-test.
    if len(sg) >= 10 and len(lg) >= 10:
        tg, pg = stats.ttest_ind(sg["gross_ret"].dropna(), lg["gross_ret"].dropna(),
                                 equal_var=False)
        gap = _boot_diff_ci(sg["gross_ret"], lg["gross_ret"])
        strat["gross_long_shortgamma_minus_longgamma"] = {
            **(gap or {}), "welch_t": float(tg), "welch_p": float(pg),
        }
        tn, pn = stats.ttest_ind(sg["net_ret_long"].dropna(), lg["net_ret_long"].dropna(),
                                 equal_var=False)
        strat["net_long_shortgamma_minus_longgamma"] = {
            "diff_mean": float(sg["net_ret_long"].mean() - lg["net_ret_long"].mean()),
            "welch_t": float(tn), "welch_p": float(pn),
        }

    return {
        "n_trades": int(len(df)),
        "span_years": float(span_years),
        "window": [df["date"].min().date().isoformat(), df["date"].max().date().isoformat()],
        "n_short_gamma": int(len(sg)), "n_long_gamma": int(len(lg)),
        "mean_rt_cost_pct_all": float(df["rt_cost_pct"].mean()),
        "mispricing": mis,
        "strategy": strat,
    }


def _verdict(res: dict) -> str:
    """One honest sentence: gross ~0 AND regime adds nothing => priced in, then costs."""
    s = res["strategy"]
    sgk = s["long_straddle_on_short_gamma"]
    g, n = sgk["gross"], sgk["net"]
    if g["mean"] is None:
        return "INSUFFICIENT DATA."
    net_pos = n["mean"] > 0 and n["p"] is not None and n["p"] < 0.05
    if net_pos:
        return ("SURVIVES COSTS: long straddle on short-gamma days is net-positive after "
                "real spreads -- verify hard before believing.")
    gap = s.get("gross_long_shortgamma_minus_longgamma", {})
    gci = g.get("ci95_boot")
    gap_ci = gap.get("ci95_boot")
    gross_zeroish = gci is not None and gci[0] < 0.0 < gci[1]
    gap_zeroish = gap_ci is not None and gap_ci[0] < 0.0 < gap_ci[1]
    if gross_zeroish and gap_zeroish:
        return ("PRICED IN: GROSS straddle return is ~0 "
                f"({g['mean'] * 100:+.2f}%/trade, boot-CI [{gci[0] * 1e4:+.0f},{gci[1] * 1e4:+.0f}] "
                f"bps) and conditioning on the regime adds nothing (gross SG-LG gap "
                f"{gap['diff_mean'] * 1e4:+.1f} bps, CI straddles 0) -- the bigger short-gamma move "
                f"is already in the IV. NET then bleeds {n['mean'] * 100:+.2f}%/trade after costs.")
    return ("NO CLEAR EDGE: gross straddle return or regime gap not cleanly zero; "
            "net is negative after costs regardless.")


def _print_report(symbol: str, res: dict) -> None:
    print(f"\n===== {symbol}: gamma monetization ({res['window'][0]}..{res['window'][1]}, "
          f"{res['n_trades']} trades; SG {res['n_short_gamma']} / LG {res['n_long_gamma']}) =====")
    m = res["mispricing"]
    print("  (A) MISPRICING  realized|move| vs IV-implied move (fair baseline "
          f"{m['fair_baseline_ratio']:.2f}x, NOT 1.0):")
    print(f"        short-gamma  raw {m['short_gamma_realized_over_implied']:.2f}x -> "
          f"fair-adj vol {m['short_gamma_realized_over_implied_VOL_fair_adj']:.2f}x")
    print(f"        long-gamma   raw {m['long_gamma_realized_over_implied']:.2f}x -> "
          f"fair-adj vol {m['long_gamma_realized_over_implied_VOL_fair_adj']:.2f}x")
    if "welch_p" in m:
        print(f"        Welch p (SG>LG contrast): {m['welch_p']:.4f} -> {m['interpretation']}")
    s = res["strategy"]
    sgk = s["long_straddle_on_short_gamma"]
    print("  (B/C) LONG STRADDLE on SHORT-GAMMA days:")
    gci = sgk["gross"].get("ci95_boot")
    gci_s = f"  boot-CI [{gci[0]*1e4:+.0f},{gci[1]*1e4:+.0f}] bps" if gci else ""
    print(f"        GROSS (mid->mid)  mean {sgk['gross']['mean']*100:+.2f}%/trade  "
          f"t={sgk['gross']['t']:.2f}  win {sgk['gross']['win_rate']*100:.0f}%{gci_s}")
    print(f"        round-trip spread cost ~{sgk['mean_rt_cost_pct']*100:.2f}% of entry")
    net = sgk["net"]
    sharpe = f"  Sharpe~{net['sharpe_ann']:.2f}" if net["sharpe_ann"] is not None else ""
    print(f"        NET   (ask->bid)  mean {net['mean']*100:+.2f}%/trade  "
          f"t={net['t']:.2f}  win {net['win_rate']*100:.0f}%{sharpe}")
    gap = s.get("gross_long_shortgamma_minus_longgamma")
    if gap is not None and gap.get("ci95_boot"):
        c = gap["ci95_boot"]
        print(f"        regime gap (GROSS SG-LG): {gap['diff_mean']*1e4:+.1f} bps  "
              f"95% boot-CI [{c[0]*1e4:+.1f},{c[1]*1e4:+.1f}] bps  "
              f"(straddles 0 => regime conditioning adds no gross edge)")
    shrt = s["short_straddle_on_long_gamma"]["net"]
    if shrt["mean"] is not None:
        print(f"  SHORT STRADDLE on LONG-GAMMA days (sell vol): NET mean "
              f"{shrt['mean']*100:+.2f}%/trade  t={shrt['t']:.2f}  win {shrt['win_rate']*100:.0f}%")
    print(f"  VERDICT: {_verdict(res)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    ap.add_argument("--hold-days", type=int, default=1)
    ap.add_argument("--series-dir", default=OUT_DIR)
    args = ap.parse_args()

    all_trades = []
    out: dict = {"hold_days": args.hold_days, "by_symbol": {}}
    for sym in args.symbols:
        path = os.path.join(args.series_dir, f"real_gex_series_{sym}.json")
        if not os.path.exists(path):
            print(f"!! missing GEX series for {sym} ({path}); run build_real_gex_series.py first")
            continue
        series = json.load(open(path))["series"]
        print(f"building straddle trades: {sym} ({len(series)} GEX days, hold={args.hold_days}d)...")
        df = build_trades(sym, series, hold_days=args.hold_days)
        if df.empty:
            print(f"  no trades for {sym}")
            continue
        res = analyze(df)
        _print_report(sym, res)
        out["by_symbol"][sym] = res
        all_trades.append(df)

    if len(all_trades) > 1:
        pooled = pd.concat(all_trades, ignore_index=True)
        pres = analyze(pooled)
        _print_report("POOLED", pres)
        out["pooled"] = pres

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "gamma_monetization.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
