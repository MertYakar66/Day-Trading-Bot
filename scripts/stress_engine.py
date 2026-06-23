#!/usr/bin/env python3
"""Numerical stress harness for the intraday engine's feature/pricing/sizing math.

This hammers the load-bearing math with EXTREME and DEGENERATE inputs to prove
the engine produces realistic, finite, reliable outputs and never look-aheads.
It does NOT modify engine code; it only calls the public module surfaces with
pathological data and asserts the documented contracts hold.

Coverage (see the section banners below):
  1. GEX  — intraday.features.gex.gamma_structure_at on pathological chains.
  2. S2   — intraday.signals.s2_zerodte_vrp.S2ZeroDteVrp pricing extremes + guards.
  3. SIZE — intraday.risk.sizing.kelly_size with extreme win/reward/risk.
  4. RPT  — metrics.build_report on a degenerate 1-day, zero-trade backtest.
  5. PIT  — appending future bars must not change a feature sampled earlier.

Run:  cd C:/Users/merty/Desktop/Day-Trading-Bot && python scripts/stress_engine.py

A PASS/FAIL table is printed and a machine-readable JSON summary is written to
  data_raw/realdata_validation/stress_results.json
The process exits non-zero if any stress case fails.
"""

from __future__ import annotations

import json
import math
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Resolve paths relative to the project root so the harness runs from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The SWE quant library lives under vendor/swe; the engine imports it lazily.
sys.path.insert(0, str(_PROJECT_ROOT / "vendor" / "swe"))
# The intraday engine package lives at the project root.
sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from intraday.backtest.engine import BacktestResult  # noqa: E402
from intraday.config import EngineConfig, RiskConfig  # noqa: E402
from intraday.contracts import (  # noqa: E402
    AssetKind,
    DataSource,
    OptionChainSeries,
    Side,
    SignalProposal,
)
from intraday.features.base import FeatureRow, latest_value  # noqa: E402
from intraday.features.gex import gamma_structure_at  # noqa: E402
from intraday.metrics import build_report  # noqa: E402
from intraday.risk.sizing import kelly_size  # noqa: E402
from intraday.signals.s2_zerodte_vrp import S2ZeroDteVrp  # noqa: E402
from intraday.timeutils import session_bounds_utc  # noqa: E402

# --------------------------------------------------------------------------- #
# Tiny test harness — plain asserts, collected into a PASS/FAIL table.
# --------------------------------------------------------------------------- #

UTC = "UTC"
ET = "America/New_York"


@dataclass
class Case:
    section: str
    name: str
    passed: bool
    detail: str = ""
    observed: dict = field(default_factory=dict)


RESULTS: list[Case] = []


def record(section: str, name: str, fn) -> None:
    """Run ``fn`` (which returns a dict of observed values or raises) and record."""
    try:
        observed = fn() or {}
        RESULTS.append(Case(section, name, True, "ok", observed))
    except AssertionError as exc:
        RESULTS.append(Case(section, name, False, f"ASSERT: {exc}",
                            {"traceback": traceback.format_exc(limit=3)}))
    except Exception as exc:  # noqa: BLE001 — a crash IS a failure for this harness
        RESULTS.append(Case(section, name, False, f"{type(exc).__name__}: {exc}",
                            {"traceback": traceback.format_exc(limit=4)}))


def is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(float(x))


# --------------------------------------------------------------------------- #
# Builders for synthetic pathological inputs
# --------------------------------------------------------------------------- #

EXPIRY = date(2026, 6, 22)          # a Monday (trading day); the 0DTE expiry
SNAP_TS = pd.Timestamp("2026-06-22 14:00:00", tz=UTC)
AVAIL_TS = pd.Timestamp("2026-06-22 14:00:01", tz=UTC)
AS_OF = pd.Timestamp("2026-06-22 14:30:00", tz=UTC)


def make_chain(rows: list[dict], *, expiry: date = EXPIRY) -> OptionChainSeries:
    """Build an OptionChainSeries from explicit per-strike rows.

    Each row provides strike/option_type/open_interest/implied_vol/spot; the
    snapshot/availability timestamps and expiration are filled in here so a
    single PIT snapshot is presented to the analyzer.
    """
    frame = pd.DataFrame(
        [
            {
                "snapshot_ts": SNAP_TS,
                "available_ts": AVAIL_TS,
                "expiration": pd.Timestamp(expiry),
                "strike": float(r["strike"]),
                "option_type": r["option_type"],
                "open_interest": r["open_interest"],
                "implied_vol": r["implied_vol"],
                "spot": float(r["spot"]),
            }
            for r in rows
        ]
    )
    return OptionChainSeries(symbol="SPX", frame=frame, source=DataSource.SYNTHETIC)


def feature_row_for_s2(
    *,
    atm_iv: float,
    rv: float,
    last_price: float = 5000.0,
    gex_total: float = 1.0e9,
    vrp: float | None = None,
    secs_to_close: float = 3600.0,
) -> FeatureRow:
    """Build a FeatureRow that lands S2's gate preconditions so its pricing math
    is actually exercised. ``as_of`` is derived from the session close minus
    ``secs_to_close`` so the strategy's seconds-to-close branch sees the value
    we intend (it recomputes from session_bounds_utc internally).
    """
    _, close_utc = session_bounds_utc(EXPIRY)
    as_of = close_utc - pd.Timedelta(seconds=secs_to_close)
    vrp_val = (atm_iv - rv) if vrp is None else vrp
    return FeatureRow(
        symbol="SPX",
        as_of=as_of,
        last_price=last_price,
        atm_iv=atm_iv,
        rv=rv,
        vrp=vrp_val,
        gex_total=gex_total,
        meta={},
    )


def make_proposal(
    *, win_prob: float, win_amount: float, loss_amount: float, ref_price: float = 5000.0
) -> SignalProposal:
    """A defined-risk structured proposal (S2-shape) with explicit economics so
    kelly_size's reward/risk/win_prob path is driven directly."""
    return SignalProposal(
        strategy_id="STRESS",
        symbol="SPX",
        ts=AS_OF,
        side=Side.SHORT,
        instrument=AssetKind.OPTION,
        ref_price=ref_price,
        target_price=ref_price,
        stop_price=ref_price,
        win_prob=win_prob,
        spread=0.0,
        win_amount=win_amount,
        loss_amount=loss_amount,
        meta={},
    )


# =========================================================================== #
# 1) GEX — gamma_structure_at on pathological chains
# =========================================================================== #
#
# Contract (from the docstring + DealerPositioningAnalyzer): return None OR a
# GammaStructure with a FINITE gex_total. Never NaN/inf, never an exception.

def _assert_gex_ok(gs, *, label: str) -> dict:
    """gs is None or a GammaStructure with finite numeric fields."""
    if gs is None:
        return {"gs": None, "label": label}
    # gex_total must be a finite real number.
    assert is_finite_number(gs.gex_total), f"{label}: gex_total not finite: {gs.gex_total!r}"
    assert not math.isnan(gs.gex_total), f"{label}: gex_total is NaN"
    # confidence is a probability-like scalar.
    assert is_finite_number(gs.confidence) and 0.0 <= gs.confidence <= 1.0, (
        f"{label}: confidence out of [0,1]: {gs.confidence!r}"
    )
    assert is_finite_number(gs.regime_multiplier), f"{label}: regime_multiplier not finite"
    # spot is echoed back and must be finite/positive in our constructions.
    assert is_finite_number(gs.spot), f"{label}: spot not finite"
    # Optional fields, when present, must be finite.
    for fld in ("flip_level", "flip_distance_pct", "nearest_call_wall", "nearest_put_wall"):
        v = getattr(gs, fld)
        if v is not None:
            assert is_finite_number(v), f"{label}: {fld} not finite: {v!r}"
    return {
        "gex_total": float(gs.gex_total),
        "regime": gs.regime.value,
        "confidence": float(gs.confidence),
        "regime_multiplier": float(gs.regime_multiplier),
        "flip_level": gs.flip_level,
        "label": label,
    }


def gex_single_strike():
    # A single ATM strike, PUT-heavy. Under +call/-put signs a put-dominant book
    # is short gamma ⇒ negative GEX. Proves the single-strike degenerate case
    # still produces correctly-signed, finite gamma (not a silent neutral).
    chain = make_chain([
        {"strike": 5000, "option_type": "C", "open_interest": 100, "implied_vol": 0.20, "spot": 5000},
        {"strike": 5000, "option_type": "P", "open_interest": 5000, "implied_vol": 0.20, "spot": 5000},
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    obs = _assert_gex_ok(gs, label="single_strike")
    assert gs is not None, "single_strike: expected a structure"
    assert gs.gex_total < 0.0, f"single_strike: put-heavy book must be -GEX, got {gs.gex_total}"
    return obs


def gex_symmetric_chain_nets_zero():
    # CONTROL: a perfectly symmetric chain (equal call/put OI & IV per strike)
    # MUST net to exactly 0 GEX — calls and puts share identical BSM gamma, and
    # the +call/-put signs cancel. This documents that the 0.0 in the other
    # degenerate cases is the CORRECT answer, not a silent failure.
    chain = make_chain([
        {"strike": k, "option_type": t, "open_interest": 1000, "implied_vol": 0.20, "spot": 5000}
        for k in (4900, 5000, 5100) for t in ("C", "P")
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    obs = _assert_gex_ok(gs, label="symmetric_nets_zero")
    assert gs is not None
    assert gs.gex_total == 0.0, f"symmetric chain must net to 0 GEX, got {gs.gex_total}"
    return obs


def gex_all_iv_zero():
    # Every IV is 0 — the analyzer must drop all rows and return a neutral/None,
    # never divide-by-zero into NaN.
    chain = make_chain([
        {"strike": k, "option_type": t, "open_interest": 500, "implied_vol": 0.0, "spot": 5000}
        for k in (4900, 5000, 5100) for t in ("C", "P")
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    obs = _assert_gex_ok(gs, label="all_iv_zero")
    # With no valid IV rows the analyzer yields gex_total == 0 (neutral).
    if gs is not None:
        assert gs.gex_total == 0.0, f"all_iv_zero: expected 0 GEX, got {gs.gex_total}"
    return obs


def gex_huge_iv():
    # IV == 5.0 is exactly the analyzer's exclusion boundary (iv < 5.0 kept);
    # mix one at 4.99 (kept) and several at/above 5.0 (dropped). Must stay finite.
    chain = make_chain([
        {"strike": 4800, "option_type": "C", "open_interest": 2000, "implied_vol": 5.0, "spot": 5000},
        {"strike": 5200, "option_type": "P", "open_interest": 2000, "implied_vol": 6.5, "spot": 5000},
        {"strike": 5000, "option_type": "C", "open_interest": 1500, "implied_vol": 4.99, "spot": 5000},
        {"strike": 5000, "option_type": "P", "open_interest": 1500, "implied_vol": 4.99, "spot": 5000},
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    return _assert_gex_ok(gs, label="huge_iv")


def gex_enormous_oi():
    # OI of 1e9 — ASYMMETRIC (calls >> puts) so the SpotGamma sign convention
    # (+calls, -puts) yields a genuinely nonzero, POSITIVE (long-gamma) GEX.
    # gex_total scales linearly with OI; assert it stays finite (no overflow to
    # inf) AND is actually nonzero & correctly signed — proving the engine
    # exercises real gamma math, not just degrades to a neutral 0.
    chain = make_chain([
        {"strike": 4900, "option_type": "C", "open_interest": int(1e9), "implied_vol": 0.18, "spot": 5000},
        {"strike": 5000, "option_type": "C", "open_interest": int(1e9), "implied_vol": 0.18, "spot": 5000},
        {"strike": 5100, "option_type": "C", "open_interest": int(1e9), "implied_vol": 0.18, "spot": 5000},
        {"strike": 4900, "option_type": "P", "open_interest": int(1e6), "implied_vol": 0.18, "spot": 5000},
        {"strike": 5000, "option_type": "P", "open_interest": int(1e6), "implied_vol": 0.18, "spot": 5000},
        {"strike": 5100, "option_type": "P", "open_interest": int(1e6), "implied_vol": 0.18, "spot": 5000},
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    obs = _assert_gex_ok(gs, label="enormous_oi")
    assert gs is not None, "enormous_oi: expected a structure, got None"
    assert abs(gs.gex_total) < 1e300, "enormous_oi: gex_total magnitude unrealistically huge"
    # Calls dominate (1e9 vs 1e6 puts) under +call/-put signs ⇒ strongly +GEX.
    assert gs.gex_total > 0.0, f"enormous_oi: call-heavy chain must be +GEX, got {gs.gex_total}"
    assert gs.regime.value == "long_gamma_dampening", (
        f"enormous_oi: call-heavy +GEX must be long-gamma, got {gs.regime.value}"
    )
    return obs


def gex_spot_far_outside():
    # Spot 50,000 but strikes clustered near 5,000 — all options deep ITM/OTM,
    # gamma ~0. Must not blow up; flip-solve over [0.7,1.3]*spot finds nothing.
    chain = make_chain([
        {"strike": k, "option_type": t, "open_interest": 800, "implied_vol": 0.22, "spot": 50000}
        for k in (4900, 5000, 5100) for t in ("C", "P")
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    return _assert_gex_ok(gs, label="spot_far_outside")


def gex_0dte_T_near_zero():
    # 0DTE: as_of is on the expiry date itself; the analyzer floors T to max(.,1)
    # days then T=max(T,1e-6) in the Greek call, so gamma stays finite (no /0).
    same_day_avail = pd.Timestamp("2026-06-22 15:55:00", tz=UTC)
    frame = pd.DataFrame([
        {
            "snapshot_ts": same_day_avail, "available_ts": same_day_avail,
            "expiration": pd.Timestamp(EXPIRY), "strike": float(k),
            "option_type": t, "open_interest": 1200, "implied_vol": 0.30, "spot": 5000.0,
        }
        for k in (4950, 5000, 5050) for t in ("C", "P")
    ])
    chain = OptionChainSeries(symbol="SPX", frame=frame, source=DataSource.SYNTHETIC)
    as_of = pd.Timestamp("2026-06-22 15:56:00", tz=UTC)
    gs = gamma_structure_at(chain, as_of, expiry=EXPIRY, ticker="SPX")
    return _assert_gex_ok(gs, label="0dte_T_near_zero")


def gex_empty_chain_returns_none():
    chain = OptionChainSeries(
        symbol="SPX",
        frame=pd.DataFrame(columns=[
            "snapshot_ts", "available_ts", "expiration", "strike",
            "option_type", "open_interest", "implied_vol", "spot",
        ]),
        source=DataSource.SYNTHETIC,
    )
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    assert gs is None, f"empty chain must return None, got {gs!r}"
    return {"gs": None}


def gex_wrong_expiry_returns_none():
    # Snapshot exists but for a different expiry → no rows for the 0DTE → None.
    chain = make_chain(
        [{"strike": 5000, "option_type": "C", "open_interest": 100, "implied_vol": 0.2, "spot": 5000}],
        expiry=date(2026, 7, 17),
    )
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    assert gs is None, f"wrong-expiry snapshot must return None, got {gs!r}"
    return {"gs": None}


def gex_negative_oi_no_crash():
    # Adversarial: a malformed feed with negative OI must not crash or NaN.
    chain = make_chain([
        {"strike": k, "option_type": t, "open_interest": -500, "implied_vol": 0.2, "spot": 5000}
        for k in (4900, 5100) for t in ("C", "P")
    ])
    gs = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")
    return _assert_gex_ok(gs, label="negative_oi")


# =========================================================================== #
# 2) S2 — pricing across extremes + honesty guards
# =========================================================================== #
#
# Contract: propose() returns None where a guard is designed to fire; when it
# DOES return a proposal, credit (win_amount) and max_loss (loss_amount) are
# finite & positive and win_prob ∈ [0.01, 0.99].

S2 = S2ZeroDteVrp()
S2CFG = EngineConfig.default()


def _assert_s2_proposal_sane(prop, *, label: str) -> dict:
    assert prop is not None, f"{label}: expected a proposal"
    assert is_finite_number(prop.win_amount) and prop.win_amount > 0, (
        f"{label}: credit not finite/positive: {prop.win_amount!r}"
    )
    assert is_finite_number(prop.loss_amount) and prop.loss_amount > 0, (
        f"{label}: max_loss not finite/positive: {prop.loss_amount!r}"
    )
    assert 0.01 <= prop.win_prob <= 0.99, f"{label}: win_prob out of [0.01,0.99]: {prop.win_prob}"
    # Sanity: structured economics consistent.
    assert prop.is_structured, f"{label}: proposal should be structured"
    return {
        "win_prob": float(prop.win_prob),
        "credit": float(prop.win_amount),
        "max_loss": float(prop.loss_amount),
        "short_width": float(prop.meta["short_width"]),
        "label": label,
    }


def s2_extreme_low_iv():
    # atm_iv = 0.01 (1% vol). To actually DRIVE the pricing math at this extreme
    # (rather than bail at the VRP gate), force vrp above threshold with a near-
    # zero but positive rv. The Black-Scholes structure at 1% vol is tiny but must
    # be finite and either price sanely or cleanly refuse on degenerate credit.
    fr = feature_row_for_s2(atm_iv=0.01, rv=0.0009, vrp=0.05)
    prop = S2.propose(fr, config=S2CFG)
    if prop is None:
        return {"result": "None (degenerate/sub-floor credit at 1% vol)"}
    return _assert_s2_proposal_sane(prop, label="extreme_low_iv")


def s2_extreme_high_iv():
    # atm_iv = 3.0 (300% vol), rv = 0.5 — huge VRP, wide strikes. Must be finite.
    fr = feature_row_for_s2(atm_iv=3.0, rv=0.5)
    prop = S2.propose(fr, config=S2CFG)
    if prop is None:
        return {"result": "None"}
    obs = _assert_s2_proposal_sane(prop, label="extreme_high_iv")
    # Width must be finite and not absurd vs spot at this horizon.
    assert math.isfinite(prop.meta["short_width"]), "high_iv: short_width not finite"
    return obs


def s2_sweep_iv():
    # Sweep atm_iv across 0.01..3.0; every outcome is None or sane. Count priced.
    priced = 0
    worst = None
    for atm_iv in [0.01, 0.05, 0.10, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0]:
        fr = feature_row_for_s2(atm_iv=atm_iv, rv=max(0.001, atm_iv * 0.3))
        prop = S2.propose(fr, config=S2CFG)
        if prop is not None:
            _assert_s2_proposal_sane(prop, label=f"sweep_iv@{atm_iv}")
            priced += 1
            worst = min(worst, prop.win_prob) if worst is not None else prop.win_prob
    return {"priced": priced, "swept": 9, "min_win_prob_seen": worst}


def s2_seconds_near_zero_guard():
    # 60s to close < min_seconds_to_close (1800) → must refuse (None).
    fr = feature_row_for_s2(atm_iv=0.5, rv=0.1, secs_to_close=60.0)
    prop = S2.propose(fr, config=S2CFG)
    assert prop is None, f"near-close guard must fire (None), got {prop!r}"
    return {"result": "None (min_seconds_to_close guard fired)"}


def s2_sigma_real_zero_guard():
    # rv == 0 → sigma_real <= 0 honesty guard must fire (None). This is the
    # documented guard that prevents win_prob inflating to the 0.99 cap.
    fr = feature_row_for_s2(atm_iv=0.5, rv=0.0)
    prop = S2.propose(fr, config=S2CFG)
    assert prop is None, f"sigma_real<=0 honesty guard must fire (None), got {prop!r}"
    return {"result": "None (sigma_real<=0 guard fired)"}


def s2_atm_iv_zero_guard():
    # atm_iv == 0 → sigma_impl <= 0 guard → None.
    fr = feature_row_for_s2(atm_iv=0.0, rv=0.1)
    # vrp would be negative here; force vrp above threshold to reach the iv guard.
    fr = FeatureRow(
        symbol="SPX", as_of=fr.as_of, last_price=5000.0,
        atm_iv=0.0, rv=0.1, vrp=0.10, gex_total=1e9, meta={},
    )
    prop = S2.propose(fr, config=S2CFG)
    assert prop is None, f"atm_iv<=0 guard must fire (None), got {prop!r}"
    return {"result": "None (atm_iv<=0 guard fired)"}


def s2_nonpositive_gex_no_trade():
    # gex_total <= 0 (short gamma) → S2 must not sell premium (None).
    fr = feature_row_for_s2(atm_iv=0.5, rv=0.1, gex_total=-1e9)
    prop = S2.propose(fr, config=S2CFG)
    assert prop is None, f"short-gamma must block S2 (None), got {prop!r}"
    return {"result": "None (gex<=0 no-trade)"}


def s2_low_vrp_no_trade():
    # VRP below threshold → None.
    fr = feature_row_for_s2(atm_iv=0.20, rv=0.199)  # vrp = 0.001 < 0.02
    prop = S2.propose(fr, config=S2CFG)
    assert prop is None, f"low-VRP must block S2 (None), got {prop!r}"
    return {"result": "None (vrp<threshold)"}


def s2_win_prob_always_bounded():
    # Across a grid that DOES price, win_prob must never escape [0.01, 0.99].
    seen = []
    for atm_iv in [0.15, 0.3, 0.6, 1.2, 2.5]:
        for rv_frac in [0.1, 0.3, 0.6, 0.9]:
            fr = feature_row_for_s2(atm_iv=atm_iv, rv=max(0.001, atm_iv * rv_frac))
            prop = S2.propose(fr, config=S2CFG)
            if prop is not None:
                assert 0.01 <= prop.win_prob <= 0.99, (
                    f"win_prob escaped bounds: {prop.win_prob} (iv={atm_iv}, rv_frac={rv_frac})"
                )
                seen.append(prop.win_prob)
    return {"n_priced": len(seen),
            "win_prob_min": min(seen) if seen else None,
            "win_prob_max": max(seen) if seen else None}


# =========================================================================== #
# 3) SIZE — kelly_size with extreme win_prob / reward / risk
# =========================================================================== #
#
# Contract: size ∈ [0, max_size]; never negative; result is finite; a degenerate
# edge yields size 0 with a reason, not a crash.

RISK = RiskConfig()


def _assert_sizing_ok(res, *, label: str) -> dict:
    assert isinstance(res.size, int), f"{label}: size not int"
    assert res.size >= 0, f"{label}: NEGATIVE size: {res.size}"
    assert res.size <= RISK.max_size, f"{label}: size exceeds max_size: {res.size}"
    assert is_finite_number(res.kelly_fraction), f"{label}: kelly_fraction not finite"
    assert res.kelly_fraction >= 0.0, f"{label}: negative kelly_fraction"
    assert is_finite_number(res.risk_dollars), f"{label}: risk_dollars not finite"
    # When size > 0 it must respect min_size.
    if res.size > 0:
        assert res.size >= RISK.min_size, f"{label}: size below min_size: {res.size}"
    return {"size": res.size, "kelly_fraction": float(res.kelly_fraction),
            "reason": res.reason, "label": label}


def size_certain_win():
    # win_prob = 0.999, big reward, small risk → strong Kelly but caps must clamp.
    p = make_proposal(win_prob=0.999, win_amount=1000.0, loss_amount=10.0)
    res = kelly_size(p, RISK)
    obs = _assert_sizing_ok(res, label="certain_win")
    # Even a near-certain edge cannot exceed max_size.
    assert res.size <= RISK.max_size
    return obs


def size_certain_loss():
    # win_prob = 0.001 → Kelly <= 0 → size 0, never negative.
    p = make_proposal(win_prob=0.001, win_amount=10.0, loss_amount=1000.0)
    res = kelly_size(p, RISK)
    obs = _assert_sizing_ok(res, label="certain_loss")
    assert res.size == 0, f"certain loss must size to 0, got {res.size}"
    return obs


def size_zero_risk():
    # loss_amount = 0 → no stop distance → size 0 (cannot divide by 0).
    p = make_proposal(win_prob=0.6, win_amount=100.0, loss_amount=0.0)
    res = kelly_size(p, RISK)
    obs = _assert_sizing_ok(res, label="zero_risk")
    assert res.size == 0, f"zero risk must size to 0, got {res.size}"
    return obs


def size_zero_reward():
    # win_amount = 0 → Kelly <= 0 (b=0) → size 0.
    p = make_proposal(win_prob=0.6, win_amount=0.0, loss_amount=100.0)
    res = kelly_size(p, RISK)
    obs = _assert_sizing_ok(res, label="zero_reward")
    assert res.size == 0, f"zero reward must size to 0, got {res.size}"
    return obs


def size_huge_reward_tiny_risk():
    # Reward 1e9, risk 1e-3 → enormous edge; notional/risk caps + max_size clamp.
    p = make_proposal(win_prob=0.7, win_amount=1e9, loss_amount=1e-3)
    res = kelly_size(p, RISK)
    obs = _assert_sizing_ok(res, label="huge_reward_tiny_risk")
    return obs


def size_extreme_win_prob_one():
    # p = 1.0 exactly — Kelly formula edge; must still produce a finite clamped size.
    p = make_proposal(win_prob=1.0, win_amount=100.0, loss_amount=100.0)
    res = kelly_size(p, RISK)
    return _assert_sizing_ok(res, label="win_prob_1.0")


def size_extreme_win_prob_zero():
    # p = 0.0 exactly — Kelly <= 0 → size 0.
    p = make_proposal(win_prob=0.0, win_amount=100.0, loss_amount=100.0)
    res = kelly_size(p, RISK)
    obs = _assert_sizing_ok(res, label="win_prob_0.0")
    assert res.size == 0
    return obs


def size_sweep_never_negative():
    # Brute sweep across the cross product of extremes — size never negative,
    # always within [0, max_size], always finite.
    n = 0
    sized = 0
    for p in [0.0, 0.01, 0.25, 0.5, 0.75, 0.99, 1.0]:
        for win in [1e-3, 1.0, 100.0, 1e6, 1e9]:
            for loss in [1e-3, 1.0, 100.0, 1e6, 1e9]:
                prop = make_proposal(win_prob=p, win_amount=win, loss_amount=loss)
                res = kelly_size(prop, RISK)
                _assert_sizing_ok(res, label=f"sweep p={p} w={win} l={loss}")
                n += 1
                if res.size > 0:
                    sized += 1
    return {"combinations": n, "nonzero_sizes": sized}


# =========================================================================== #
# 4) RPT — build_report on a degenerate 1-day, zero-trade backtest
# =========================================================================== #
#
# Contract (the inception-fix path): a single-day, zero-trade run must yield a
# report with NO NaN Sharpe/Sortino/volatility — they are pinned to 0.0.

def _degenerate_result(*, n_days: int, with_curve: bool) -> BacktestResult:
    cfg = EngineConfig.default()
    nav = cfg.risk.paper_nav
    curve = []
    if with_curve:
        for k in range(n_days):
            day = pd.Timestamp("2026-06-22") + pd.Timedelta(days=k)
            curve.append({"date": day.isoformat(), "portfolio_value": nav})
    return BacktestResult(
        config=cfg,
        symbols=["SPX"],
        interval="1m",
        data_source=DataSource.SYNTHETIC,
        initial_capital=nav,
        final_equity=nav,
        trades=[],          # zero trades
        fills=[],
        equity_curve=curve,
        signals=[],
        n_days=n_days,
    )


def report_one_day_zero_trades():
    res = _degenerate_result(n_days=1, with_curve=True)
    rep = build_report(res)
    # The inception-fix contract: 1-day risk stats are 0.0, never NaN.
    for fld in ("sharpe_ratio", "sortino_ratio", "volatility"):
        v = getattr(rep, fld)
        assert is_finite_number(v), f"{fld} not finite: {v!r}"
        assert not math.isnan(v), f"{fld} is NaN (inception bug)"
        assert v == 0.0, f"{fld} must be 0.0 on a 1-day run, got {v}"
    # All headline numbers finite.
    for fld in ("total_return", "annualized_return", "max_drawdown",
                "calmar_ratio", "net_pnl", "win_rate", "profit_factor"):
        v = getattr(rep, fld)
        assert is_finite_number(v), f"{fld} not finite: {v!r}"
    assert rep.total_trades == 0
    return {"sharpe": rep.sharpe_ratio, "sortino": rep.sortino_ratio,
            "vol": rep.volatility, "n_days": rep.n_days, "trades": rep.total_trades}


def report_zero_day_empty_curve():
    # Pathological: n_days=0 with empty curve — must not crash, no NaN.
    res = _degenerate_result(n_days=0, with_curve=False)
    rep = build_report(res)
    for fld in ("sharpe_ratio", "sortino_ratio", "volatility", "max_drawdown",
                "total_return", "annualized_return"):
        v = getattr(rep, fld)
        assert is_finite_number(v), f"{fld} not finite: {v!r}"
        assert not math.isnan(v), f"{fld} is NaN"
    return {"sharpe": rep.sharpe_ratio, "vol": rep.volatility, "n_days": rep.n_days}


def report_two_day_zero_trades():
    # 2 days, flat curve, no trades → real-stats path runs; Sharpe of a flat
    # curve is 0/0 territory in SWE — must still be finite (not NaN).
    res = _degenerate_result(n_days=2, with_curve=True)
    rep = build_report(res)
    for fld in ("sharpe_ratio", "sortino_ratio", "volatility"):
        v = getattr(rep, fld)
        assert is_finite_number(v), f"{fld} not finite on flat 2-day curve: {v!r}"
        assert not math.isnan(v), f"{fld} is NaN on flat 2-day curve"
    return {"sharpe": rep.sharpe_ratio, "sortino": rep.sortino_ratio, "vol": rep.volatility}


# =========================================================================== #
# 5) PIT — appending future bars must not change a feature sampled earlier
# =========================================================================== #
#
# Contract (DESIGN §2.3): latest_value(series, as_of) and BarSeries.available_at
# read only data at/<= the cutoff, so appending future rows is a no-op for a
# value sampled at an earlier as_of.

def _make_bar_series_close(n: int = 400):
    idx = pd.date_range("2026-06-22 13:31:00", periods=n, freq="1min", tz=UTC)
    rng = np.random.default_rng(7)
    # A plausible mean-reverting-ish close path around 5000.
    close = 5000.0 + np.cumsum(rng.normal(0, 0.5, size=n))
    return pd.Series(close, index=idx, name="close")


def pit_latest_value_ignores_future():
    s = _make_bar_series_close(400)
    i = 180
    as_of = s.index[i]
    lat = pd.Timedelta(milliseconds=250)
    base = latest_value(s, as_of, lat)
    # Append 200 future bars far beyond as_of.
    fut_idx = pd.date_range(s.index[-1] + pd.Timedelta(minutes=1),
                            periods=200, freq="1min", tz=UTC)
    fut = pd.Series(np.linspace(9000, 9100, 200), index=fut_idx, name="close")
    extended = pd.concat([s, fut])
    after = latest_value(extended, as_of, lat)
    assert base == after, f"PIT leak: latest_value changed {base} -> {after} after appending future"
    # Also: a value stamped t is invisible until t + latency.
    j = 100
    t = s.index[j]
    before_arrival = latest_value(s, t + lat - pd.Timedelta(milliseconds=1), lat)
    assert before_arrival == s.iloc[j - 1], "latency leak: bar visible before t+latency"
    at_arrival = latest_value(s, t + lat, lat)
    assert at_arrival == s.iloc[j], "latency error: bar not visible at t+latency"
    return {"sampled_value": float(base), "unchanged_after_append": True}


def pit_barseries_available_at_ignores_future():
    from intraday.contracts import BAR_COLUMNS, BarSeries
    n = 300
    idx = pd.date_range("2026-06-22 13:31:00", periods=n, freq="1min", tz=UTC)
    rng = np.random.default_rng(11)
    close = 5000.0 + np.cumsum(rng.normal(0, 0.4, size=n))
    frame = pd.DataFrame({
        "open": close, "high": close + 0.3, "low": close - 0.3,
        "close": close, "volume": rng.integers(1, 1000, size=n).astype(float),
    }, index=idx)[list(BAR_COLUMNS)]
    bs = BarSeries(symbol="SPX", interval="1m", frame=frame,
                   source=DataSource.SYNTHETIC, latency=pd.Timedelta(milliseconds=250))
    i = 150
    as_of = idx[i] + bs.latency
    snap_before = bs.available_at(as_of).copy()

    # Append future bars and rebuild an identical BarSeries.
    fut_idx = pd.date_range(idx[-1] + pd.Timedelta(minutes=1), periods=120, freq="1min", tz=UTC)
    fut_close = np.linspace(8000, 8100, 120)
    fut = pd.DataFrame({
        "open": fut_close, "high": fut_close + 0.3, "low": fut_close - 0.3,
        "close": fut_close, "volume": np.full(120, 500.0),
    }, index=fut_idx)[list(BAR_COLUMNS)]
    ext = BarSeries(symbol="SPX", interval="1m", frame=pd.concat([frame, fut]),
                    source=DataSource.SYNTHETIC, latency=bs.latency)
    snap_after = ext.available_at(as_of)

    assert len(snap_before) == len(snap_after), (
        f"PIT leak: available_at row count changed {len(snap_before)} -> {len(snap_after)}"
    )
    assert snap_before.index.equals(snap_after.index), "PIT leak: available_at index changed"
    assert np.allclose(snap_before["close"].to_numpy(), snap_after["close"].to_numpy()), (
        "PIT leak: available_at close values changed after appending future bars"
    )
    # And the last visible bar must be <= cutoff (close + latency <= as_of).
    cutoff = as_of - bs.latency
    assert snap_after.index.max() <= cutoff, "available_at returned a bar past the cutoff"
    return {"rows_visible": int(len(snap_after)),
            "last_visible_ts": str(snap_after.index.max())}


def pit_gex_feature_ignores_future_snapshots():
    # A GEX feature sampled at as_of must be identical whether or not LATER
    # snapshots exist in the chain. We exercise the real feature surface.
    # Asymmetric (call-heavy) so the sampled GEX is a real nonzero value — a
    # PIT leak would actually change it, making this a meaningful invariance test.
    base_rows = [
        {"strike": k, "option_type": "C", "open_interest": 3000, "implied_vol": 0.2, "spot": 5000}
        for k in (4900, 5000, 5100)
    ] + [
        {"strike": k, "option_type": "P", "open_interest": 800, "implied_vol": 0.2, "spot": 5000}
        for k in (4900, 5000, 5100)
    ]
    chain = make_chain(base_rows)  # one snapshot available at AVAIL_TS
    gs_before = gamma_structure_at(chain, AS_OF, expiry=EXPIRY, ticker="SPX")

    # Append a LATER snapshot (available after AS_OF) with very different OI.
    later_avail = AS_OF + pd.Timedelta(minutes=30)
    later = pd.DataFrame([
        {
            "snapshot_ts": later_avail, "available_ts": later_avail,
            "expiration": pd.Timestamp(EXPIRY), "strike": float(k),
            "option_type": t, "open_interest": 999_999, "implied_vol": 0.9, "spot": 5200.0,
        }
        for k in (4900, 5000, 5100) for t in ("C", "P")
    ])
    ext = OptionChainSeries(symbol="SPX",
                            frame=pd.concat([chain.frame, later], ignore_index=True),
                            source=DataSource.SYNTHETIC)
    gs_after = gamma_structure_at(ext, AS_OF, expiry=EXPIRY, ticker="SPX")

    assert (gs_before is None) == (gs_after is None), "PIT leak: presence changed"
    assert gs_before is not None and gs_before.gex_total != 0.0, (
        "PIT-GEX setup must yield a nonzero baseline GEX for a meaningful test"
    )
    if gs_before is not None:
        assert gs_before.gex_total == gs_after.gex_total, (
            f"PIT leak: gex_total changed {gs_before.gex_total} -> {gs_after.gex_total} "
            "after appending a future snapshot"
        )
        assert gs_before.spot == gs_after.spot, "PIT leak: spot changed after future snapshot"
    return {"gex_total": (gs_before.gex_total if gs_before else None),
            "unchanged_after_future_snapshot": True}


# =========================================================================== #
# Runner
# =========================================================================== #

def main() -> int:
    sections = [
        ("GEX", [
            ("single_strike_put_heavy_neg", gex_single_strike),
            ("symmetric_chain_nets_zero", gex_symmetric_chain_nets_zero),
            ("all_iv_zero", gex_all_iv_zero),
            ("huge_iv_5.0", gex_huge_iv),
            ("enormous_oi_1e9", gex_enormous_oi),
            ("spot_far_outside", gex_spot_far_outside),
            ("0dte_T_near_zero", gex_0dte_T_near_zero),
            ("empty_chain_None", gex_empty_chain_returns_none),
            ("wrong_expiry_None", gex_wrong_expiry_returns_none),
            ("negative_oi_no_crash", gex_negative_oi_no_crash),
        ]),
        ("S2", [
            ("extreme_low_iv_0.01", s2_extreme_low_iv),
            ("extreme_high_iv_3.0", s2_extreme_high_iv),
            ("sweep_iv_0.01..3.0", s2_sweep_iv),
            ("seconds_near_0_guard", s2_seconds_near_zero_guard),
            ("sigma_real_0_guard", s2_sigma_real_zero_guard),
            ("atm_iv_0_guard", s2_atm_iv_zero_guard),
            ("nonpositive_gex_no_trade", s2_nonpositive_gex_no_trade),
            ("low_vrp_no_trade", s2_low_vrp_no_trade),
            ("win_prob_always_bounded", s2_win_prob_always_bounded),
        ]),
        ("SIZE", [
            ("certain_win_clamped", size_certain_win),
            ("certain_loss_zero", size_certain_loss),
            ("zero_risk_zero", size_zero_risk),
            ("zero_reward_zero", size_zero_reward),
            ("huge_reward_tiny_risk", size_huge_reward_tiny_risk),
            ("win_prob_1.0", size_extreme_win_prob_one),
            ("win_prob_0.0", size_extreme_win_prob_zero),
            ("sweep_never_negative", size_sweep_never_negative),
        ]),
        ("RPT", [
            ("one_day_zero_trades_no_nan_sharpe", report_one_day_zero_trades),
            ("zero_day_empty_curve", report_zero_day_empty_curve),
            ("two_day_zero_trades_no_nan", report_two_day_zero_trades),
        ]),
        ("PIT", [
            ("latest_value_ignores_future", pit_latest_value_ignores_future),
            ("barseries_available_at_ignores_future", pit_barseries_available_at_ignores_future),
            ("gex_feature_ignores_future_snapshots", pit_gex_feature_ignores_future_snapshots),
        ]),
    ]

    for section, cases in sections:
        for name, fn in cases:
            record(section, name, fn)

    # --- PASS/FAIL table ---------------------------------------------------- #
    print("=" * 78)
    print(" NUMERICAL STRESS HARNESS — intraday engine feature/pricing/sizing math")
    print("=" * 78)
    width = max(len(f"{c.section}/{c.name}") for c in RESULTS) + 2
    cur_section = None
    for c in RESULTS:
        if c.section != cur_section:
            cur_section = c.section
            print(f"\n[{c.section}]")
        status = "PASS" if c.passed else "FAIL"
        label = f"{c.name}"
        print(f"  {status}  {label:<{width}}  {c.detail}")

    total = len(RESULTS)
    passed = sum(1 for c in RESULTS if c.passed)
    failed = total - passed
    print("\n" + "-" * 78)
    print(f" TOTAL: {passed}/{total} passed, {failed} failed")
    print("-" * 78)

    # --- JSON summary ------------------------------------------------------- #
    out_dir = Path("data_raw/realdata_validation")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "stress_results.json"
    summary = {
        "harness": "stress_engine",
        "generated_at_utc": pd.Timestamp.now(tz=UTC).isoformat(),
        "totals": {"total": total, "passed": passed, "failed": failed},
        "sections": {},
        "cases": [
            {
                "section": c.section,
                "name": c.name,
                "passed": c.passed,
                "detail": c.detail,
                "observed": c.observed,
            }
            for c in RESULTS
        ],
        "failures": [
            {"section": c.section, "name": c.name, "detail": c.detail,
             "observed": c.observed}
            for c in RESULTS if not c.passed
        ],
    }
    for c in RESULTS:
        s = summary["sections"].setdefault(c.section, {"total": 0, "passed": 0})
        s["total"] += 1
        s["passed"] += int(c.passed)
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f" JSON written: {out_path.resolve()}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
