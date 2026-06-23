"""Hermetic tests for intraday.data.straddle_history (real-NBBO ATM-straddle quote
extraction from the deep option history). Network-free; builds in-memory partitions
from Black-Scholes-consistent prices so parity spot + IV inversion are exact
round-trips and no real SWE data is required.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from engine.option_pricer import black_scholes_price

from intraday.data import straddle_history as sh
from intraday.data.swe_offline import SweDataUnavailable

# Self-consistent world: spot 600, 30 DTE, flat 20% IV, SPY rate/div.
_SPOT = 600.0
_DAY = date(2026, 3, 2)
_EXPIRY = date(2026, 4, 1)
_R = 0.04
_Q = 0.013
_IV = 0.20
_T = (pd.Timestamp(_EXPIRY) - pd.Timestamp(_DAY)).days / 365.0


def _row(strike, right, bid, ask, created, oi=1000.0):
    return {"strike": float(strike), "right": right, "bid": bid, "ask": ask,
            "open_interest": oi, "created": created}


def _make_partition(days=(_DAY,), iv=_IV, strikes=None, *, spread=0.01) -> pd.DataFrame:
    """EOD partition whose mids are exact BS prices at ``iv``, for one or more days."""
    if strikes is None:
        strikes = np.arange(560.0, 641.0, 5.0)
    rows = []
    for d in days:
        created = datetime(d.year, d.month, d.day, 16, 0)
        for k in strikes:
            for right in ("CALL", "PUT"):
                price = max(black_scholes_price(_SPOT, float(k), _T, _R, iv, right.lower(), q=_Q), 0.01)
                rows.append(_row(k, right, price * (1 - spread), price * (1 + spread), created))
    df = pd.DataFrame(rows)
    df["created_date"] = pd.to_datetime(df["created"]).dt.date
    return df


def test_atm_quote_recovers_spot_iv_and_legs():
    part = _make_partition()
    qte = sh.atm_straddle_quote("SPY", _DAY, _EXPIRY, r=_R, q=_Q, partition=part)
    assert qte is not None
    assert qte.strike == 600.0                       # ATM = nearest listed strike to spot
    assert qte.parity_spot == pytest.approx(_SPOT, abs=0.2)
    assert qte.atm_iv == pytest.approx(_IV, abs=2e-3)
    # Legs match the constructed NBBO and the BS mid.
    bs_call = black_scholes_price(_SPOT, 600.0, _T, _R, _IV, "call", q=_Q)
    assert qte.call_mid == pytest.approx(bs_call, rel=1e-9)
    assert qte.call_ask > qte.call_bid > 0
    assert qte.dte == 30


def test_pnl_invariants():
    qte = sh.atm_straddle_quote("SPY", _DAY, _EXPIRY, r=_R, q=_Q, partition=_make_partition())
    assert qte is not None
    assert qte.straddle_mid == pytest.approx(qte.call_mid + qte.put_mid)
    assert qte.buy_cost == pytest.approx(qte.call_ask + qte.put_ask)
    assert qte.sell_proceeds == pytest.approx(qte.call_bid + qte.put_bid)
    # You always pay more to open than you receive to close at the same instant.
    assert qte.buy_cost > qte.straddle_mid > qte.sell_proceeds


def test_force_strike_pins_and_missing_strike_none():
    part = _make_partition()
    pinned = sh.atm_straddle_quote("SPY", _DAY, _EXPIRY, r=_R, q=_Q, partition=part,
                                   force_strike=590.0)
    assert pinned is not None and pinned.strike == 590.0
    # A strike not in the grid → None (never approximated).
    assert sh.atm_straddle_quote("SPY", _DAY, _EXPIRY, r=_R, q=_Q, partition=part,
                                 force_strike=12345.0) is None


def test_refuses_spx():
    part = _make_partition()
    for bad in ("SPX", "SPXW"):
        with pytest.raises(SweDataUnavailable):
            sh.atm_straddle_quote(bad, _DAY, _EXPIRY, r=_R, partition=part)


def test_missing_day_returns_none():
    assert sh.atm_straddle_quote("SPY", date(2099, 1, 1), _EXPIRY, r=_R, q=_Q,
                                 partition=_make_partition()) is None


def test_one_sided_chain_returns_none():
    # Only calls → no two-sided pair → no ATM straddle.
    created = datetime(2026, 3, 2, 16, 0)
    rows = [_row(k, "CALL", 5.0, 5.1, created) for k in (595.0, 600.0, 605.0)]
    part = pd.DataFrame(rows)
    part["created_date"] = pd.to_datetime(part["created"]).dt.date
    assert sh.atm_straddle_quote("SPY", _DAY, _EXPIRY, r=_R, q=_Q, partition=part) is None


def test_pivot_filters_junk_quotes():
    created = datetime(2026, 3, 2, 16, 0)
    rows = [
        # good two-sided pair at 600
        _row(600.0, "CALL", 9.0, 9.2, created), _row(600.0, "PUT", 8.0, 8.2, created),
        # one-sided (bid<=0) at 605 → dropped
        _row(605.0, "CALL", 0.0, 6.0, created), _row(605.0, "PUT", 5.0, 5.2, created),
        # crossed (ask<=bid) at 610 → dropped
        _row(610.0, "CALL", 7.0, 6.5, created), _row(610.0, "PUT", 4.0, 4.2, created),
        # absurd spread at 595 (>60% cap) → dropped
        _row(595.0, "CALL", 1.0, 20.0, created), _row(595.0, "PUT", 9.0, 9.2, created),
    ]
    part = pd.DataFrame(rows)
    part["created_date"] = pd.to_datetime(part["created"]).dt.date
    piv = sh._pivot_two_sided(part.loc[part["created_date"] == _DAY])
    assert list(piv.index) == [600.0]               # only the clean strike survives


def test_next_trading_day_and_gap():
    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]
    part = _make_partition(days=days)
    assert sh.next_trading_day(part, date(2026, 3, 2), n=1) == date(2026, 3, 3)
    assert sh.next_trading_day(part, date(2026, 3, 2), n=2) == date(2026, 3, 4)
    assert sh.next_trading_day(part, date(2026, 3, 5), n=1) is None  # nothing after last
    assert sh.trading_day_gap(part, date(2026, 3, 2), date(2026, 3, 4)) == 2
    assert sh.trading_day_gap(part, date(2026, 3, 2), date(2026, 3, 3)) == 1


def test_entry_exit_same_contract_roundtrip():
    # Two consecutive days at the SAME flat IV: entry picks ATM, exit pins that
    # strike, and both recover the same spot → a clean 1-day hold with zero drift.
    days = [date(2026, 3, 2), date(2026, 3, 3)]
    part = _make_partition(days=days)
    entry = sh.atm_straddle_quote("SPY", days[0], _EXPIRY, r=_R, q=_Q, partition=part)
    assert entry is not None
    xday = sh.next_trading_day(part, days[0], n=1)
    assert xday == days[1]
    exit_q = sh.atm_straddle_quote("SPY", xday, _EXPIRY, r=_R, q=_Q, partition=part,
                                   force_strike=entry.strike)
    assert exit_q is not None
    assert exit_q.strike == entry.strike
    assert exit_q.parity_spot == pytest.approx(entry.parity_spot, abs=0.2)
