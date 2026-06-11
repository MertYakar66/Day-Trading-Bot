"""Tests for intraday.features.ofi (direction-signed order-flow imbalance).

Each print is signed by what the customer initiation means for the UNDERLYING:
buy-call / sell-put = bullish (+), sell-call / buy-put = bearish (−), "mid"
excluded. OFI = (bull_vol - bear_vol) / (bull_vol + bear_vol) over PIT-filtered
prints. Pure functions + the SyntheticDataProvider tape; no network,
deterministic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.features.ofi import ofi_at, ofi_from_prints
from intraday.timeutils import session_bounds_utc


def _prints(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Build a minimal prints frame: (side_inferred, right, size)."""
    return pd.DataFrame(
        {
            "side_inferred": [r[0] for r in rows],
            "right": [r[1] for r in rows],
            "size": [r[2] for r in rows],
        }
    )


# --------------------------------------------------------------------------- #
# ofi_from_prints — direction signing (the audit-confirmed G9 fix)
# --------------------------------------------------------------------------- #


def test_buying_calls_is_bullish():
    df = _prints([("buy", "C", 3.0), ("buy", "C", 7.0)])
    assert ofi_from_prints(df) == pytest.approx(1.0)


def test_buying_puts_is_bearish_not_buying_pressure():
    """THE defect this fix exists for: heavy put BUYING is bearish, but the old
    sign-blind OFI counted it as +1 'buying pressure'."""
    df = _prints([("buy", "P", 5.0), ("buy", "P", 2.0)])
    assert ofi_from_prints(df) == pytest.approx(-1.0)


def test_selling_puts_is_bullish():
    df = _prints([("sell", "P", 4.0)])
    assert ofi_from_prints(df) == pytest.approx(1.0)


def test_selling_calls_is_bearish():
    df = _prints([("sell", "C", 4.0)])
    assert ofi_from_prints(df) == pytest.approx(-1.0)


def test_call_buying_offsets_put_buying():
    # buy 10 calls (bull) vs buy 30 puts (bear) -> (10-30)/40 = -0.5.
    # The old definition read this as +1.0 (all customer "buys").
    df = _prints([("buy", "C", 10.0), ("buy", "P", 30.0)])
    assert ofi_from_prints(df) == pytest.approx(-0.5)


def test_mixed_size_weighted_not_count_weighted():
    # Three bullish prints of size 1 vs one bearish of size 9 -> (3-9)/12 = -0.5.
    df = _prints([("buy", "C", 1.0), ("buy", "C", 1.0), ("sell", "P", 1.0),
                  ("sell", "C", 9.0)])
    assert ofi_from_prints(df) == pytest.approx((3.0 - 9.0) / 12.0)


def test_balanced_flow_is_zero():
    df = _prints([("buy", "C", 4.0), ("buy", "P", 4.0)])
    assert ofi_from_prints(df) == pytest.approx(0.0)


def test_mid_prints_are_excluded_from_classification():
    # 'mid' is ambiguous and must not enter either side, whatever its right.
    df = _prints([("buy", "C", 6.0), ("mid", "C", 100.0), ("mid", "P", 50.0),
                  ("sell", "C", 2.0)])
    assert ofi_from_prints(df) == pytest.approx((6.0 - 2.0) / 8.0)


def test_right_is_normalized():
    # 'call'/'put' spellings and stray whitespace must classify like 'C'/'P'.
    df = _prints([("buy", "call", 5.0), ("buy", " put ", 5.0)])
    assert ofi_from_prints(df) == pytest.approx(0.0)


def test_empty_frame_returns_none():
    assert ofi_from_prints(_prints([])) is None


def test_none_input_returns_none():
    assert ofi_from_prints(None) is None


def test_all_mid_returns_none():
    # denom == 0 -> None, not a divide-by-zero / NaN.
    df = _prints([("mid", "C", 5.0), ("mid", "P", 11.0)])
    assert ofi_from_prints(df) is None


def test_zero_size_prints_yield_none():
    df = _prints([("buy", "C", 0.0), ("sell", "C", 0.0)])
    assert ofi_from_prints(df) is None


def test_output_bounded_in_unit_interval():
    df = _prints([("buy", "C", 1.0), ("sell", "P", 2.0), ("sell", "C", 8.0),
                  ("buy", "P", 3.0)])
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
