"""Tests for intraday.features.gex.gamma_structure_at (SWE-backed gamma spine).

These exercise the dealer GEX / gamma-flip / regime feature against the
deterministic synthetic option chain. They require the SWE
``engine.dealer_positioning`` module, so the whole module is marked ``swe``.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from intraday.contracts import GammaRegime
from intraday.features.gex import GammaStructure, gamma_structure_at
from intraday.timeutils import session_bounds_utc

pytestmark = pytest.mark.swe


def test_gamma_structure_after_first_snapshot(spy_chain, day):
    """After a snapshot is PIT-available, we get a well-formed GammaStructure."""
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=2)

    gs = gamma_structure_at(spy_chain, as_of, expiry=day, ticker="SPY")

    assert isinstance(gs, GammaStructure)

    # Spot is the underlying price at the snapshot: positive and SPY-sized.
    assert gs.spot > 0
    assert 100.0 < gs.spot < 2000.0

    # Net dollar GEX is a finite real number (sign carries the regime meaning).
    assert math.isfinite(gs.gex_total)

    # Regime is one of the defined dealer-gamma regimes.
    assert isinstance(gs.regime, GammaRegime)
    assert gs.regime in set(GammaRegime)

    # The regime multiplier is the documented attenuation band [0.70, 1.05].
    assert 0.70 <= gs.regime_multiplier <= 1.05

    # Confidence is a probability-like scalar in [0, 1].
    assert 0.0 <= gs.confidence <= 1.0

    # as_of is preserved as a tz-aware UTC timestamp.
    assert gs.as_of == pd.Timestamp(as_of)
    assert gs.as_of.tz is not None


def test_gamma_structure_none_before_any_snapshot(spy_chain, day):
    """At day start (no snapshot has arrived) the feature returns None."""
    day_start = pd.Timestamp(day).tz_localize("UTC")
    assert gamma_structure_at(spy_chain, day_start, expiry=day, ticker="SPY") is None

    # The RTH open itself is still before the first available snapshot (latency),
    # so it must also be None.
    open_utc, _ = session_bounds_utc(day)
    assert gamma_structure_at(spy_chain, open_utc, expiry=day, ticker="SPY") is None


def test_gamma_structure_optional_levels_consistent(spy_chain, day):
    """Walls/flip levels, when present, are positive prices straddling spot-ish."""
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=3)

    gs = gamma_structure_at(spy_chain, as_of, expiry=day, ticker="SPY")
    assert gs is not None

    for level in (gs.flip_level, gs.nearest_call_wall, gs.nearest_put_wall):
        if level is not None:
            assert level > 0

    if gs.flip_distance_pct is not None:
        assert math.isfinite(gs.flip_distance_pct)

    # A near-flip / non-neutral regime should not contradict a finite flip level
    # when one is reported.
    if gs.flip_level is not None and gs.flip_distance_pct is not None:
        # distance% is roughly (spot - flip)/spot in magnitude; sanity-bound it.
        assert abs(gs.flip_distance_pct) < 100.0


def test_gamma_structure_deterministic(spy_chain, day):
    """Same chain + same as_of yields identical structure (deterministic synth)."""
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=2)

    a = gamma_structure_at(spy_chain, as_of, expiry=day, ticker="SPY")
    b = gamma_structure_at(spy_chain, as_of, expiry=day, ticker="SPY")

    assert a is not None and b is not None
    assert a.spot == pytest.approx(b.spot)
    assert a.gex_total == pytest.approx(b.gex_total)
    assert a.regime is b.regime
    assert a.regime_multiplier == pytest.approx(b.regime_multiplier)
