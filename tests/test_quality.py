"""Liquidity / freshness predicate tests (DESIGN §6 liquidity gate, §2.4 freshness)."""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.data.quality import assess_liquidity, is_stale, spread_bps


def test_spread_bps_basic():
    assert spread_bps(0.075, 750.0) == pytest.approx(1.0)  # 0.075/750 = 1 bp
    assert spread_bps(0.0, 750.0) == 0.0


def test_spread_bps_guards_bad_inputs():
    assert spread_bps(0.4, 0.0) == float("inf")
    assert spread_bps(-0.1, 750.0) == float("inf")


def test_assess_liquidity_tight_ok():
    chk = assess_liquidity(0.4, 750.0, max_spread_bps=25.0)  # ~5.3 bps
    assert chk.ok
    assert chk.spread_bps == pytest.approx(spread_bps(0.4, 750.0))


def test_assess_liquidity_wide_flagged():
    chk = assess_liquidity(5.0, 750.0, max_spread_bps=25.0)  # ~66 bps
    assert not chk.ok
    assert "bps" in chk.reason


def test_is_stale_freshness():
    now = pd.Timestamp("2026-05-18T15:00:00Z")
    fresh = now - pd.Timedelta(seconds=1)
    old = now - pd.Timedelta(minutes=5)
    assert not is_stale(fresh, now, max_age=pd.Timedelta(seconds=2))
    assert is_stale(old, now, max_age=pd.Timedelta(seconds=2))
