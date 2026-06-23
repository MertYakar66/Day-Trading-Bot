"""REAL-DATA GEX validation harness (NETWORK-FREE).

Validates the dealer gamma structure produced by the engine against REAL,
defect-corrected SWE index option chains. For each GEX-ready index symbol and
each EOD snapshot date it:

  1. Loads the real chain (nearest expiry only) via
     ``intraday.data.swe_offline.load_index_chain`` and the REAL prior-session
     3-month risk-free rate via ``...load_risk_free_curve``.
  2. Computes the dealer gamma structure at the REAL expiry and REAL rate via
     ``intraday.features.gex.gamma_structure_at`` (which wraps
     ``engine.dealer_positioning.DealerPositioningAnalyzer``).
  3. RECONCILES ``gex_total`` to an INDEPENDENT Black-Scholes gamma recompute:
        gex = sum_strikes sign * OI * 100 * spot^2 * 0.01 * gamma(BSM)
     with gamma recomputed from scipy ``norm`` (and cross-checked against
     ``engine.option_pricer.black_scholes_all_greeks``). The relative error
     should be ~machine epsilon because the engine reconstructs the same gamma.
  4. CHECKS realism / internal consistency:
        - nearest call/put walls land on genuinely high-OI strikes
        - flip level sits BELOW spot in a long-gamma regime
        - GEX sign matches the regime label and the magnitude is economically
          sane for the symbol (SPX on the order of +$ billions / 1%).

NDX is a raw quote-only snapshot (no iv / greeks) and is skipped with a logged
note. VIX has no 2026-04-23 capture; missing (symbol, date) pairs are recorded
as skips rather than failures.

Run:
    cd C:/Users/merty/Desktop/Day-Trading-Bot && python scripts/validate_real_gex.py

Writes a JSON report to
``data_raw/realdata_validation/gex_validation.json`` and prints a table.
This script is read-only against the SWE data store; it never opens a socket.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

# The intraday package lives at the repo root; the SWE engine it wraps
# (engine.dealer_positioning, engine.option_pricer) lives under vendor/swe.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, "vendor/swe")
sys.path.insert(0, str(_REPO / "vendor" / "swe"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# Independent cross-check pricer (same engine BSM, used to corroborate the
# closed-form scipy gamma below — a second witness on the reconciliation).
from engine.option_pricer import black_scholes_all_greeks  # type: ignore  # noqa: E402
from scipy.stats import norm  # noqa: E402

from intraday.data.swe_offline import (  # noqa: E402
    SweDataUnavailable,
    load_index_chain,
    load_risk_free_curve,
)
from intraday.features.gex import gamma_structure_at  # noqa: E402

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# GEX-ready greek chains. NDX is quote-only and deliberately excluded (skipped
# with a logged note). SPXW is the weekly book; SPX is the canonical monthly
# index chain the harness validates. We keep the operator-named set.
GEX_SYMBOLS = ("SPX", "RUT", "VIX", "XSP", "DJX")
SKIP_SYMBOLS = {"NDX": "quote-only snapshot (no iv/greeks/OI) — not GEX-ready"}
SNAPSHOT_DATES = (date(2026, 4, 23), date(2026, 5, 24), date(2026, 6, 1))

# Economic-sanity magnitude bands for |gex_total| (dollars per 1% move). These
# are deliberately wide order-of-magnitude rails — a structure outside its band
# is flagged for human eyes, not silently failed. SPX should be $billions.
GEX_MAGNITUDE_BANDS = {
    "SPX": (1e8, 5e10),   # large-cap index, deep OI → $0.1bn..$50bn
    "RUT": (1e6, 1e10),
    "DJX": (1e3, 1e9),    # DJX is 1/100 of the Dow, tiny notional per contract
    "XSP": (1e4, 1e9),    # mini-SPX, 1/10 of SPX
    "VIX": (1e4, 5e9),    # VIX options on a ~16 underlying
}

OUT_PATH = _REPO / "data_raw" / "realdata_validation" / "gex_validation.json"


# --------------------------------------------------------------------------- #
# Independent Black-Scholes gamma (closed form, scipy norm)
# --------------------------------------------------------------------------- #
def bsm_gamma(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    """Black-Scholes gamma (per share), independent of the engine code path."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    return float(np.exp(-q * T) * norm.pdf(d1) / (S * sigma * sqrt_T))


def independent_gex(
    snap_frame: pd.DataFrame,
    expiry: date,
    as_of: pd.Timestamp,
    risk_free_rate: float,
) -> tuple[float, float]:
    """Recompute total dealer GEX from scratch, mirroring the analyzer's math.

    Returns ``(gex_scipy, gex_engine_pricer)`` — the first from the closed-form
    scipy gamma above, the second using ``black_scholes_all_greeks`` strike by
    strike as a second independent witness. The two must agree, and either must
    agree with ``gamma_structure_at``'s ``gex_total``.

    Sign convention matches LONG_CALLS_SHORT_PUTS: calls +1, puts -1.
    Per-strike contribution: sign * gamma * OI * 100 * spot^2 * 0.01.
    """
    spot = float(snap_frame["spot"].iloc[0])
    # The analyzer anchors T to the resolved as_of *date* and floors at 1 day.
    as_of_date = pd.Timestamp(as_of).tz_convert("UTC").tz_localize(None).date()
    T = max((expiry - as_of_date).days, 1) / 365.0

    df = snap_frame.copy()
    df["ot"] = df["option_type"].astype(str).str.upper().str.strip().str[0]
    # Mirror the analyzer's row filter exactly: strike>0, 0<iv<5, OI present.
    df = df[
        (df["strike"] > 0)
        & df["implied_vol"].notna()
        & (df["implied_vol"] > 0)
        & (df["implied_vol"] < 5.0)
        & df["open_interest"].notna()
    ]
    df = df[df["ot"].isin(["C", "P"])]

    gex_scipy = 0.0
    gex_pricer = 0.0
    for strike, sub in df.groupby("strike"):
        K = float(strike)
        for ot, sign in (("C", 1), ("P", -1)):
            rows = sub[sub["ot"] == ot]
            if len(rows) == 0:
                continue
            iv = float(rows["implied_vol"].mean())  # analyzer averages IV per strike×type
            if iv <= 0 or iv >= 5.0:
                continue
            oi = int(rows["open_interest"].sum())
            g_scipy = bsm_gamma(spot, K, T, risk_free_rate, iv)
            try:
                g_pricer = float(
                    black_scholes_all_greeks(
                        S=spot,
                        K=K,
                        T=max(T, 1e-6),
                        r=risk_free_rate,
                        sigma=iv,
                        option_type=("call" if ot == "C" else "put"),
                        q=0.0,
                    )["gamma"]
                )
            except Exception:
                g_pricer = 0.0
            factor = sign * oi * 100 * spot * spot * 0.01
            gex_scipy += g_scipy * factor
            gex_pricer += g_pricer * factor
    return gex_scipy, gex_pricer


# --------------------------------------------------------------------------- #
# Wall / OI consistency
# --------------------------------------------------------------------------- #
def wall_oi_rank(snap_frame: pd.DataFrame, wall_strike: float | None) -> dict | None:
    """Where does ``wall_strike`` rank by total OI among all strikes?

    Returns the wall strike's combined OI, the percentile rank of that OI among
    strikes, and the absolute OI rank (1 = highest). A real gamma wall should
    sit on a high-OI strike, so we expect a high percentile.
    """
    if wall_strike is None:
        return None
    df = snap_frame.copy()
    oi_by_strike = df.groupby("strike")["open_interest"].sum()
    if oi_by_strike.empty:
        return None
    wall_oi = float(oi_by_strike.get(float(wall_strike), 0.0))
    # Percentile of this strike's OI within the OI distribution.
    pct = float((oi_by_strike <= wall_oi).mean())
    rank = int((oi_by_strike > wall_oi).sum()) + 1  # 1-based, 1 = top
    return {
        "strike": float(wall_strike),
        "wall_oi": wall_oi,
        "max_strike_oi": float(oi_by_strike.max()),
        "oi_percentile": pct,
        "oi_rank": rank,
        "n_strikes": int(oi_by_strike.size),
        # A high-OI strike: in the top ~25% of the OI distribution.
        "is_high_oi": bool(pct >= 0.75),
    }


# --------------------------------------------------------------------------- #
# Per (symbol, date) validation
# --------------------------------------------------------------------------- #
def validate_one(symbol: str, snap: date, curve) -> dict:
    result: dict = {"symbol": symbol, "snapshot_date": snap.isoformat()}
    try:
        chain, expiry = load_index_chain(symbol, snap)
    except SweDataUnavailable as exc:
        result["status"] = "skipped"
        result["reason"] = str(exc).splitlines()[0]
        return result

    # REAL prior-session 3-month risk-free rate (PIT: strictly prior session).
    r3m = curve.rate_on(snap, "rate_3m", prior_session=True)

    # as_of: just after the chain becomes available (EOD capture + latency).
    as_of = pd.Timestamp(chain.frame["available_ts"].iloc[0]) + pd.Timedelta(minutes=1)

    gs = gamma_structure_at(chain, as_of, expiry=expiry, ticker=symbol, risk_free_rate=r3m)
    if gs is None:
        result["status"] = "skipped"
        result["reason"] = "gamma_structure_at returned None (no snapshot/expiry rows)"
        return result

    # The exact single-snapshot, single-expiry frame the analyzer consumed.
    snap_frame = chain.latest_available(as_of)
    snap_frame = snap_frame.loc[
        pd.to_datetime(snap_frame["expiration"]).dt.date == expiry
    ].copy()

    spot = float(gs.spot)
    n_calls = int((snap_frame["option_type"].astype(str).str.upper().str[0] == "C").sum())
    n_puts = int((snap_frame["option_type"].astype(str).str.upper().str[0] == "P").sum())
    total_oi = int(snap_frame["open_interest"].sum())

    # --- (1) Independent BSM reconciliation ---------------------------------
    gex_scipy, gex_pricer = independent_gex(snap_frame, expiry, as_of, r3m)
    denom = abs(gs.gex_total) if abs(gs.gex_total) > 0 else 1.0
    rel_err_scipy = abs(gex_scipy - gs.gex_total) / denom
    rel_err_pricer = abs(gex_pricer - gs.gex_total) / denom
    # Tolerance: the recompute mirrors the analyzer's gamma exactly, so this is
    # floating-point noise. 1e-9 is generous headroom over machine epsilon.
    recon_ok = bool(rel_err_scipy < 1e-9 and rel_err_pricer < 1e-9)

    # --- (2) Wall / OI consistency ------------------------------------------
    call_wall = wall_oi_rank(snap_frame, gs.nearest_call_wall)
    put_wall = wall_oi_rank(snap_frame, gs.nearest_put_wall)
    # Walls (when present) should land on high-OI strikes. None is acceptable
    # (no wall within near_wall_pct of spot) and is not a failure.
    walls_present = [w for w in (call_wall, put_wall) if w is not None]
    walls_ok = bool(walls_present) and all(w["is_high_oi"] for w in walls_present)

    # --- (3) Economic sanity -------------------------------------------------
    regime = gs.regime.value if hasattr(gs.regime, "value") else str(gs.regime)
    long_gamma = regime == "long_gamma_dampening"
    short_gamma = regime == "short_gamma_amplifying"
    # Sign ⇔ regime label.
    sign_matches_regime = bool(
        (gs.gex_total > 0 and (long_gamma or regime == "near_flip"))
        or (gs.gex_total < 0 and (short_gamma or regime == "near_flip"))
        or gs.gex_total == 0
    )
    # In a long-gamma regime the flip sits below spot (dealers flip to short
    # gamma as price falls toward the put-heavy strikes).
    flip_below_spot = (
        bool(gs.flip_level is not None and gs.flip_level < spot) if long_gamma else None
    )
    lo, hi = GEX_MAGNITUDE_BANDS.get(symbol, (0.0, float("inf")))
    mag = abs(gs.gex_total)
    magnitude_sane = bool(lo <= mag <= hi)

    # Overall internal-consistency verdict for this cell.
    checks = {
        "reconciliation": recon_ok,
        "walls_on_high_oi": walls_ok,
        "sign_matches_regime": sign_matches_regime,
        "magnitude_sane": magnitude_sane,
    }
    # flip_below_spot only asserted in a long-gamma regime.
    if long_gamma:
        checks["flip_below_spot"] = bool(flip_below_spot)
    cell_ok = all(checks.values())

    result.update(
        {
            "status": "ok" if cell_ok else "flagged",
            "expiry": expiry.isoformat(),
            "dte": (expiry - as_of.tz_convert("UTC").tz_localize(None).date()).days,
            "spot": spot,
            "risk_free_3m": float(r3m),
            "n_calls": n_calls,
            "n_puts": n_puts,
            "total_oi": total_oi,
            # Engine output
            "gex_total": float(gs.gex_total),
            "regime": regime,
            "confidence": float(gs.confidence),
            "flip_level": gs.flip_level,
            "flip_distance_pct": gs.flip_distance_pct,
            "nearest_call_wall": gs.nearest_call_wall,
            "nearest_put_wall": gs.nearest_put_wall,
            "regime_multiplier": float(gs.regime_multiplier),
            # (1) Reconciliation
            "reconciliation": {
                "gex_independent_scipy": gex_scipy,
                "gex_independent_engine_pricer": gex_pricer,
                "rel_err_scipy": rel_err_scipy,
                "rel_err_engine_pricer": rel_err_pricer,
                "passed": recon_ok,
            },
            # (2) Walls
            "call_wall_oi": call_wall,
            "put_wall_oi": put_wall,
            # (3) Sanity
            "checks": checks,
        }
    )
    return result


def main() -> int:
    curve = load_risk_free_curve()
    print(f"[setup] risk-free curve {curve.start}..{curve.end} (rate_3m, prior-session PIT)")

    rows: list[dict] = []
    notes: list[str] = []
    for sym, why in SKIP_SYMBOLS.items():
        notes.append(f"SKIP {sym}: {why}")
        print(f"[skip] {sym}: {why}")

    for sym in GEX_SYMBOLS:
        for snap in SNAPSHOT_DATES:
            res = validate_one(sym, snap, curve)
            rows.append(res)
            if res["status"] == "skipped":
                print(f"[skip] {sym} {snap}: {res.get('reason', '')[:80]}")

    # ----- Aggregate verdict -------------------------------------------------
    evaluated = [r for r in rows if r["status"] in ("ok", "flagged")]
    n_ok = sum(1 for r in evaluated if r["status"] == "ok")
    n_flagged = sum(1 for r in evaluated if r["status"] == "flagged")
    n_skipped = sum(1 for r in rows if r["status"] == "skipped")
    max_rel_err = max(
        (r["reconciliation"]["rel_err_scipy"] for r in evaluated),
        default=float("nan"),
    )
    all_recon = all(r["reconciliation"]["passed"] for r in evaluated) if evaluated else False

    report = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "swe_root_default": True,
        "symbols_evaluated": GEX_SYMBOLS,
        "symbols_skipped": SKIP_SYMBOLS,
        "snapshot_dates": [d.isoformat() for d in SNAPSHOT_DATES],
        "summary": {
            "n_evaluated": len(evaluated),
            "n_ok": n_ok,
            "n_flagged": n_flagged,
            "n_skipped": n_skipped,
            "max_reconciliation_rel_err": max_rel_err,
            "all_reconciliations_passed": all_recon,
        },
        "notes": notes,
        "cells": rows,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # ----- Print table -------------------------------------------------------
    print()
    hdr = (
        f"{'SYM':<4} {'DATE':<10} {'EXP':<10} {'DTE':>3} {'SPOT':>9} "
        f"{'GEX($)':>14} {'REGIME':<22} {'FLIP':>9} {'CALLwall':>9} {'PUTwall':>9} "
        f"{'RELERR':>9} {'STATUS':<8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if r["status"] == "skipped":
            print(
                f"{r['symbol']:<4} {r['snapshot_date']:<10} {'-':<10} {'-':>3} {'-':>9} "
                f"{'-':>14} {'(skipped)':<22} {'-':>9} {'-':>9} {'-':>9} {'-':>9} "
                f"{r['status']:<8}"
            )
            continue
        gex = r["gex_total"]
        gex_s = f"{gex/1e9:+.3f}bn" if abs(gex) >= 1e8 else f"{gex/1e6:+.2f}M"
        flip = f"{r['flip_level']:.0f}" if r["flip_level"] is not None else "none"
        cw = f"{r['nearest_call_wall']:.0f}" if r["nearest_call_wall"] is not None else "none"
        pw = f"{r['nearest_put_wall']:.0f}" if r["nearest_put_wall"] is not None else "none"
        relerr = r["reconciliation"]["rel_err_scipy"]
        print(
            f"{r['symbol']:<4} {r['snapshot_date']:<10} {r['expiry']:<10} {r['dte']:>3} "
            f"{r['spot']:>9.1f} {gex_s:>14} {r['regime']:<22} {flip:>9} {cw:>9} {pw:>9} "
            f"{relerr:>9.1e} {r['status']:<8}"
        )

    print()
    print(
        f"[summary] evaluated={len(evaluated)} ok={n_ok} flagged={n_flagged} "
        f"skipped={n_skipped} | max recon rel-err={max_rel_err:.2e} | "
        f"all reconciliations passed={all_recon}"
    )
    print(f"[report] {OUT_PATH}")
    # Non-zero exit only if a reconciliation actually broke (the load-bearing
    # invariant). Economic "flagged" cells are surfaced, not hard-failed.
    return 0 if all_recon else 1


if __name__ == "__main__":
    raise SystemExit(main())
