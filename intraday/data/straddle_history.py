"""Extract real ATM-straddle NBBO quotes from SWE's deep index option history.

This is the bid/ask-level companion to :mod:`intraday.data.index_chain_history`.
Where that module reconstructs a *whole chain* (mids → parity spot → BS-inverted IV
→ GEX), this one pulls the **raw NBBO bid/ask** of the single ATM call+put so a
backtest can price an ATM straddle with *real* transaction cost (cross the spread),
not a modelled one.

Used by the gamma-monetization test (``scripts/test_gamma_monetization.py``): does
the dealer-gamma regime's validated next-day-move effect survive being *traded* as a
straddle, net of the real spread? The clean separation of gross (mid→mid) vs net
(ask→bid) P&L is what turns "real signal, but loses to costs" from an assertion into
a measurement.

Honest scope (mirrors :mod:`index_chain_history`):
- **SPY/QQQ only**; SPX/SPXW refused (settlement ambiguity).
- A quote is **refused (returns None)**, never approximated, when the ATM strike is
  not two-sided-quoted with a sane spread — absence is reported, not guessed.
- EOD snapshots only (the deep history is daily); ``T = max(dte,1)/365`` calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .index_chain_history import (
    _MAX_PLAUSIBLE_IV,
    _MAX_REL_SPREAD,
    PER_SYMBOL_Q,
    REFUSED_ROOTS,
    _eod_snapshot,
    _read_partition,
    list_history_expiries,
)
from .parity import implied_spot
from .swe_offline import SweDataUnavailable


@dataclass(frozen=True)
class StraddleQuote:
    """Real NBBO quote for the ATM call+put of one ``(symbol, day, expiry)``."""

    symbol: str
    day: date
    expiry: date
    dte: int
    strike: float
    parity_spot: float          # put-call-parity spot from THIS strike's C/P pair
    call_bid: float
    call_ask: float
    put_bid: float
    put_ask: float
    call_oi: int
    put_oi: int
    atm_iv: float | None        # mean of BS-inverted call/put IV (None if neither inverts)

    @property
    def call_mid(self) -> float:
        return 0.5 * (self.call_bid + self.call_ask)

    @property
    def put_mid(self) -> float:
        return 0.5 * (self.put_bid + self.put_ask)

    @property
    def straddle_mid(self) -> float:
        """Fair (mid) value of the straddle — no transaction cost."""
        return self.call_mid + self.put_mid

    @property
    def buy_cost(self) -> float:
        """Cost to OPEN a long straddle: pay BOTH asks (cross the spread)."""
        return self.call_ask + self.put_ask

    @property
    def sell_proceeds(self) -> float:
        """Proceeds to CLOSE a long straddle (or open a short): hit BOTH bids."""
        return self.call_bid + self.put_bid


def _pivot_two_sided(snap: pd.DataFrame) -> pd.DataFrame:
    """Per-strike call/put bid/ask/mid/OI, keeping only sane two-sided quotes.

    Returns a frame indexed by ``strike`` with columns
    ``c_bid c_ask p_bid p_ask c_oi p_oi`` for strikes that have BOTH a call and a
    put passing ``bid>0 & ask>bid`` and the relative-spread sanity cap.
    """
    q = snap.copy()
    q["right"] = q["right"].astype(str).str.upper()
    q = q.loc[(q["bid"] > 0) & (q["ask"] > q["bid"])]
    if q.empty:
        return pd.DataFrame()
    q["mid"] = 0.5 * (q["bid"] + q["ask"])
    q = q.loc[(q["ask"] - q["bid"]) <= _MAX_REL_SPREAD * q["mid"].clip(lower=1e-6) * 2]
    if q.empty:
        return pd.DataFrame()
    if "open_interest" not in q.columns:
        q["open_interest"] = 0
    piv = q.pivot_table(
        index="strike",
        columns="right",
        values=["bid", "ask", "mid", "open_interest"],
        aggfunc="last",
    )
    needed = [("bid", "CALL"), ("ask", "CALL"), ("bid", "PUT"), ("ask", "PUT")]
    if any(col not in piv.columns for col in needed):
        return pd.DataFrame()
    out = pd.DataFrame(
        {
            "c_bid": piv[("bid", "CALL")],
            "c_ask": piv[("ask", "CALL")],
            "c_mid": piv[("mid", "CALL")],
            "p_bid": piv[("bid", "PUT")],
            "p_ask": piv[("ask", "PUT")],
            "p_mid": piv[("mid", "PUT")],
            "c_oi": piv.get(("open_interest", "CALL")),
            "p_oi": piv.get(("open_interest", "PUT")),
        }
    ).dropna(subset=["c_bid", "c_ask", "p_bid", "p_ask"])
    return out


def atm_straddle_quote(
    symbol: str,
    day: date,
    expiry: date,
    *,
    r: float,
    q: float | None = None,
    root: str | None = None,
    partition: pd.DataFrame | None = None,
    force_strike: float | None = None,
) -> StraddleQuote | None:
    """Real NBBO ATM-straddle quote for ``(symbol, day, expiry)``.

    The ATM strike is the listed strike nearest the put-call-parity spot (computed
    from the nearest-forward two-sided pair), unless ``force_strike`` pins it (used
    to value the SAME contract on the exit day). ``r`` is the real risk-free rate
    for ``day``; ``q`` defaults to the per-symbol dividend yield. Returns ``None``
    when the snapshot is missing or the chosen strike is not sanely two-sided —
    never an approximation.
    """
    from engine.option_pricer import implied_volatility  # lazy (SWE)

    sym = symbol.upper()
    if sym in REFUSED_ROOTS:
        raise SweDataUnavailable(
            f"{sym} straddle refused (settlement ambiguity); use SPY/QQQ"
        )
    if q is None:
        q = PER_SYMBOL_Q.get(sym, 0.0)

    if partition is None:
        expiries = {e: p for e, p in list_history_expiries(sym, root)}
        if expiry not in expiries:
            return None
        partition = _read_partition(expiries[expiry])

    snap = _eod_snapshot(partition, day)
    if snap.empty:
        return None
    dte = (pd.Timestamp(expiry) - pd.Timestamp(day)).days
    if dte < 0:
        return None
    T = max(dte, 1) / 365.0

    piv = _pivot_two_sided(snap)
    if piv.empty:
        return None
    strikes = piv.index.to_numpy(dtype=float)

    # Choose the strike: forced (exit) or ATM by parity spot (entry).
    if force_strike is not None:
        if float(force_strike) not in piv.index:
            return None
        strike = float(force_strike)
    else:
        # Parity spot from the nearest-forward pair (smallest |c_mid - p_mid|).
        gap = (piv["c_mid"] - piv["p_mid"]).abs()
        near = piv.loc[gap.nsmallest(min(3, len(piv))).index]
        spots = [
            implied_spot(float(row["c_mid"]), float(row["p_mid"]), float(k), r=r, q=q, T=T)
            for k, row in near.iterrows()
        ]
        spots = [float(s) for s in spots if s is not None and np.isfinite(s) and s > 0]
        if not spots:
            return None
        spot_sel = float(np.median(spots))
        strike = float(strikes[int(np.argmin(np.abs(strikes - spot_sel)))])

    row = piv.loc[strike]
    c_bid, c_ask = float(row["c_bid"]), float(row["c_ask"])
    p_bid, p_ask = float(row["p_bid"]), float(row["p_ask"])
    c_mid, p_mid = float(row["c_mid"]), float(row["p_mid"])

    # Parity spot from THIS strike's own pair (consistent entry/exit underlying).
    parity = implied_spot(c_mid, p_mid, strike, r=r, q=q, T=T)
    if parity is None or not np.isfinite(parity) or parity <= 0:
        return None
    parity = float(parity)

    # BS-invert ATM IV from each leg's mid; average the valid ones.
    ivs: list[float] = []
    for price, otype in ((c_mid, "call"), (p_mid, "put")):
        iv = implied_volatility(price, parity, strike, T, r, otype, q=q)
        if iv is not None and np.isfinite(iv) and 0 < iv < _MAX_PLAUSIBLE_IV:
            ivs.append(float(iv))
    atm_iv = float(np.mean(ivs)) if ivs else None

    def _oi(val: object) -> int:
        try:
            f = float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return int(f) if np.isfinite(f) else 0

    return StraddleQuote(
        symbol=sym, day=day, expiry=expiry, dte=int(dte), strike=strike,
        parity_spot=parity,
        call_bid=c_bid, call_ask=c_ask, put_bid=p_bid, put_ask=p_ask,
        call_oi=_oi(row.get("c_oi")), put_oi=_oi(row.get("p_oi")),
        atm_iv=atm_iv,
    )


def next_trading_day(partition: pd.DataFrame, day: date, n: int = 1) -> date | None:
    """The ``n``-th trading day strictly after ``day`` present in ``partition`` (or None)."""
    later = sorted(d for d in partition["created_date"].unique() if d > day)
    if len(later) < n:
        return None
    return later[n - 1]


def trading_day_gap(partition: pd.DataFrame, start: date, end: date) -> int:
    """Count of partition trading days in ``(start, end]`` — the real hold length."""
    return int(sum(1 for d in partition["created_date"].unique() if start < d <= end))
