"""Liquidity / freshness predicate tests (DESIGN §6 liquidity gate, §2.4 freshness)."""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.data.provider import FeedGapError
from intraday.data.quality import (
    assert_no_feed_gap,
    assess_liquidity,
    detect_feed_gap,
    is_stale,
    spread_bps,
)


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


def test_no_feed_gap_on_contiguous_bars(spy_bars):
    """The synthetic 1-minute session is contiguous: no gap, no raise."""
    assert detect_feed_gap(spy_bars.frame.index, "1m") is None
    assert_no_feed_gap(spy_bars.frame.index, "1m")  # does not raise


def test_feed_gap_detected_and_halts(spy_bars):
    """Dropping a bar creates a >1.5×interval gap that must be flagged + halt."""
    gapped = spy_bars.frame.drop(spy_bars.frame.index[100])
    gap = detect_feed_gap(gapped.index, "1m")
    assert gap is not None
    before, after, delta = gap
    assert delta == pd.Timedelta(minutes=2)
    with pytest.raises(FeedGapError):
        assert_no_feed_gap(gapped.index, "1m")


def test_provenance_round_trips(store, provider, day):
    """Stored tape/chain read back with their real source; a missing sidecar
    refuses to guess (so real data can never be silently relabelled synthetic)."""
    from intraday.contracts import DataSource

    tape = provider.get_option_tape("SPY", day)
    chain = provider.get_option_chain("SPY", day)
    store.write_tape(tape, day)
    store.write_chain(chain, day)
    assert store.read_tape("SPY", day).source is DataSource.SYNTHETIC
    assert store.read_chain("SPY", day).source is DataSource.SYNTHETIC

    # Delete the sidecar → read refuses rather than assuming a source.
    part = store._partition("option_tape", "SPY", day)
    (part / "trades_meta.json").unlink()
    with pytest.raises(FileNotFoundError):
        store.read_tape("SPY", day)
