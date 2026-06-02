"""Tests for intraday.features.ofi (order-flow imbalance from the option tape).

OFI = (buy_vol - sell_vol) / (buy_vol + sell_vol) over PIT-filtered prints.
Pure functions + the SyntheticDataProvider tape; no network, deterministic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.features.ofi import ofi_at, ofi_from_prints
from intraday.timeutils import session_bounds_utc


def _prints(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a minimal prints frame with the columns ofi_from_prints reads."""
    return pd.DataFrame(
        {
            "side_inferred": [r[0] for r in rows],
            "size": [r[1] for r in rows],
        }
    )


# --------------------------------------------------------------------------- #
# ofi_from_prints — pure-function edge cases
# --------------------------------------------------------------------------- #


def test_all_buys_is_plus_one():
    df = _prints([("buy", 3.0), ("buy", 7.0), ("buy", 1.0)])
    assert ofi_from_prints(df) == pytest.approx(1.0)


def test_all_sells_is_minus_one():
    df = _prints([("sell", 5.0), ("sell", 2.0)])
    assert ofi_from_prints(df) == pytest.approx(-1.0)


def test_mixed_is_strictly_between():
    # buy_vol=10, sell_vol=30 -> (10-30)/40 = -0.5
    df = _prints([("buy", 10.0), ("sell", 30.0)])
    val = ofi_from_prints(df)
    assert -1.0 < val < 1.0
    assert val == pytest.approx(-0.5)


def test_mixed_size_weighted_not_count_weighted():
    # Three buys of size 1 (vol 3) vs one sell of size 9 (vol 9).
    # Count-weighted would be positive; size-weighted is (3-9)/12 = -0.5.
    df = _prints([("buy", 1.0), ("buy", 1.0), ("buy", 1.0), ("sell", 9.0)])
    assert ofi_from_prints(df) == pytest.approx((3.0 - 9.0) / 12.0)


def test_balanced_buys_and_sells_is_zero():
    df = _prints([("buy", 4.0), ("sell", 4.0)])
    assert ofi_from_prints(df) == pytest.approx(0.0)


def test_mid_prints_are_excluded_from_classification():
    # 'mid' is ambiguous and must not enter buy/sell volume.
    df = _prints([("buy", 6.0), ("mid", 100.0), ("sell", 2.0)])
    # denom excludes the mid size -> (6-2)/(6+2) = 0.5
    assert ofi_from_prints(df) == pytest.approx(0.5)


def test_empty_frame_returns_none():
    assert ofi_from_prints(_prints([])) is None


def test_none_input_returns_none():
    assert ofi_from_prints(None) is None


def test_all_mid_returns_none():
    # denom (buy_vol + sell_vol) == 0 -> None, not a divide-by-zero / NaN.
    df = _prints([("mid", 5.0), ("mid", 11.0)])
    assert ofi_from_prints(df) is None


def test_zero_size_prints_yield_none():
    # Non-empty but all volume is zero -> denom 0 -> None.
    df = _prints([("buy", 0.0), ("sell", 0.0)])
    assert ofi_from_prints(df) is None


def test_output_bounded_in_unit_interval():
    df = _prints([("buy", 1.0), ("buy", 2.0), ("sell", 8.0), ("sell", 3.0)])
    val = ofi_from_prints(df)
    assert -1.0 <= val <= 1.0


# --------------------------------------------------------------------------- #
# ofi_at — PIT (no look-ahead) behavior over the synthetic tape
# --------------------------------------------------------------------------- #


@pytest.mark.no_lookahead
def test_ofi_at_value_in_range_or_none(spy_tape, day):
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=2)
    val = ofi_at(spy_tape, as_of, pd.Timedelta(minutes=5))
    assert val is None or (-1.0 <= val <= 1.0)


@pytest.mark.no_lookahead
def test_ofi_at_matches_window_prints(spy_tape, day):
    # ofi_at must equal ofi_from_prints over exactly the PIT window it uses.
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=2)
    lookback = pd.Timedelta(minutes=5)
    window = spy_tape.window_available_at(as_of, lookback)
    assert ofi_at(spy_tape, as_of, lookback) == pytest.approx(
        ofi_from_prints(window)
    )


@pytest.mark.no_lookahead
def test_ofi_at_only_uses_available_and_in_window_prints(spy_tape, day):
    # Every print feeding the computation must be (a) available by as_of and
    # (b) within the lookback — i.e. no future or stale data leaks in.
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=3)
    lookback = pd.Timedelta(minutes=5)
    window = spy_tape.window_available_at(as_of, lookback)
    assert (window["available_ts"] <= as_of).all()
    assert (window["ts"] > as_of - lookback).all()
    assert (window["ts"] <= as_of).all()


@pytest.mark.no_lookahead
def test_ofi_at_before_session_has_no_prints(spy_tape, day):
    # No trades have occurred (or are available) an hour before the open.
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc - pd.Timedelta(hours=1)
    assert ofi_at(spy_tape, as_of, pd.Timedelta(minutes=5)) is None


@pytest.mark.no_lookahead
def test_ofi_at_is_deterministic(spy_tape, day):
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=2)
    lookback = pd.Timedelta(minutes=5)
    a = ofi_at(spy_tape, as_of, lookback)
    b = ofi_at(spy_tape, as_of, lookback)
    assert a == b


@pytest.mark.no_lookahead
def test_ofi_at_longer_lookback_includes_at_least_as_many_prints(spy_tape, day):
    # Monotonicity of the PIT window: a wider lookback can only add prints.
    open_utc, _ = session_bounds_utc(day)
    as_of = open_utc + pd.Timedelta(hours=2)
    short = spy_tape.window_available_at(as_of, pd.Timedelta(minutes=5))
    long = spy_tape.window_available_at(as_of, pd.Timedelta(minutes=30))
    assert len(long) >= len(short)
    # And ofi_at over the wider window stays well-defined within bounds.
    val = ofi_at(spy_tape, as_of, pd.Timedelta(minutes=30))
    assert val is None or (-1.0 <= val <= 1.0)
