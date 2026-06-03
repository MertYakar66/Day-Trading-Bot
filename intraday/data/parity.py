"""Put-call-parity underlying reconstruction (deep-history underlying path).

IBKR intraday history is shallow (~1–2 yrs) and period-back-from-now, so for the
2022→today backtest the **underlying** is reconstructed from synchronized ATM
call/put option quotes (which Theta STANDARD does cover, 2016→today), via
put-call parity:

    F = K + e^{rT}·(C − P)            (forward implied by a same-strike C/P pair)
    S = F·e^{−(r−q)T}                 (spot from the forward; ≈ F for 0DTE)

where ``C``/``P`` are the call/put mid prices at strike ``K`` and expiry, ``T`` is
the year-fraction to expiry, ``r`` the risk-free rate, ``q`` the dividend yield.

Theta is NOT pulled this session, so this module is **proven by tests**: we
construct parity-consistent quotes from a known spot path and assert recovery to
floating-point tolerance. When the operator runs the scoped Theta pull, the same
:class:`ParityUnderlyingProvider` reconstructs deep-history underlying bars (no
volume — parity yields price, not volume; VWAP then degrades to an unweighted
cumulative mean, exactly as for an index).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import numpy as np
import pandas as pd

from ..config import EngineConfig, SessionConfig
from ..contracts import BAR_COLUMNS, BarSeries, DataSource, OptionChainSeries, OptionTape
from ..logging_config import get_logger
from ..timeutils import bar_close_index, parse_interval, session_bounds_utc
from ..timeutils import trading_days as _td
from ._remap import GridCoverage
from .provider import DataProvider, DataUnavailable

logger = get_logger(__name__)

_YEAR_SECONDS = 365.25 * 24 * 3600.0


# --------------------------------------------------------------------------- #
# Parity math (scalars + vectorized)
# --------------------------------------------------------------------------- #
def forward_from_parity(call_mid, put_mid, strike, *, r: float, T):
    """Forward price implied by a same-strike call/put pair: ``K + e^{rT}(C−P)``."""
    return strike + np.exp(r * np.asarray(T, dtype="float64")) * (
        np.asarray(call_mid, dtype="float64") - np.asarray(put_mid, dtype="float64")
    )


def spot_from_forward(forward, *, r: float, q: float, T):
    """Spot from the forward: ``F·e^{−(r−q)T}`` (≈ F as T→0)."""
    return np.asarray(forward, dtype="float64") * np.exp(-(r - q) * np.asarray(T, dtype="float64"))


def implied_spot(call_mid, put_mid, strike, *, r: float, q: float, T):
    """Underlying spot implied by an ATM call/put pair via parity (vectorized)."""
    fwd = forward_from_parity(call_mid, put_mid, strike, r=r, T=T)
    return spot_from_forward(fwd, r=r, q=q, T=T)


def year_fraction(ts, expiry: date, *, session: SessionConfig | None = None) -> float:
    """Year-fraction from ``ts`` (UTC) to the expiry's RTH close (clamped ≥ 0)."""
    _, close_utc = session_bounds_utc(expiry, session or SessionConfig())
    secs = (pd.Timestamp(close_utc) - pd.Timestamp(ts)).total_seconds()
    return max(secs, 0.0) / _YEAR_SECONDS


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #
def reconstruct_spot(
    quotes: pd.DataFrame,
    *,
    r: float,
    q: float = 0.0,
    session: SessionConfig | None = None,
) -> pd.Series:
    """Reconstruct a spot series from synchronized ATM call/put quotes.

    ``quotes`` is indexed by quote timestamp (tz-aware UTC) with columns
    ``strike``, ``call_mid``, ``put_mid``, ``expiry`` (a ``date``). Returns a spot
    :class:`pandas.Series` indexed by the same timestamps.
    """
    required = {"strike", "call_mid", "put_mid", "expiry"}
    missing = required - set(quotes.columns)
    if missing:
        raise DataUnavailable(f"parity quotes missing columns: {sorted(missing)}")
    if quotes.empty:
        return pd.Series(dtype="float64")
    ts_index = pd.DatetimeIndex(quotes.index)
    T = np.array(
        [year_fraction(ts, exp, session=session) for ts, exp in zip(ts_index, quotes["expiry"])],
        dtype="float64",
    )
    spot = implied_spot(
        quotes["call_mid"].to_numpy(dtype="float64"),
        quotes["put_mid"].to_numpy(dtype="float64"),
        quotes["strike"].to_numpy(dtype="float64"),
        r=r, q=q, T=T,
    )
    return pd.Series(spot, index=ts_index, name="spot")


def spot_to_bars(
    spot: pd.Series,
    symbol: str,
    day: date,
    interval: str,
    *,
    session: SessionConfig | None = None,
    latency: pd.Timedelta | None = None,
    min_coverage: float = 0.8,
) -> BarSeries:
    """Resample a reconstructed spot point-series into canonical-grid OHLC bars.

    No volume is available from parity (price, not size) → volume is 0, like an
    index. The frame is reindexed onto :func:`bar_close_index` and ffilled.

    Coverage discipline (mirrors the IBKR/Yahoo :mod:`intraday.data._remap` path):
    we measure how much of the canonical grid is backed by a REAL reconstructed
    quote BEFORE forward-filling, and refuse the session if that fraction is below
    ``min_coverage``. Without this, a session reconstructed from a handful of quotes
    would be ffilled to a full grid and silently presented as solid
    ``[REAL DATA: parity]`` — the exact mislabelling the store-provenance guards
    exist to prevent. Interior/trailing gaps under the floor are still ffilled
    (PIT-safe, past-only) but logged.
    """
    session = session or SessionConfig()
    latency = latency if latency is not None else pd.Timedelta(milliseconds=250)
    step = parse_interval(interval)
    target = bar_close_index(day, interval, session)
    if spot.empty:
        raise DataUnavailable(f"no parity spot to build bars for {symbol} {day}")
    agg = spot.sort_index().resample(step, label="right", closed="right").agg(
        ["first", "max", "min", "last"]
    )
    # Coverage of the canonical grid by REAL reconstructed quotes (pre-ffill).
    reindexed = agg.reindex(target)
    present_mask = reindexed["last"].notna()
    present = int(present_mask.sum())
    expected = len(target)
    leading_missing = int(present_mask.to_numpy().argmax()) if present else expected
    cov = GridCoverage(symbol.upper(), day, expected, present,
                       expected - present - leading_missing, leading_missing)
    if present == 0 or cov.coverage < min_coverage:
        raise DataUnavailable(
            f"parity {symbol} {day}: grid coverage {cov.coverage:.0%} "
            f"({present}/{expected}) below {min_coverage:.0%} - refusing to present a "
            "mostly-reconstructed session as solid bars (would mislabel ffill as real)."
        )
    # Forward-fill only (carry the last known reconstructed spot forward — PIT-safe).
    # We deliberately do NOT back-fill: a LEADING gap (no quote before the first
    # grid slot) would otherwise pull a FUTURE spot backward into a tradeable bar
    # (the same look-ahead fixed on the IBKR path). If the open is uncovered we
    # refuse the session rather than reconstruct it from the future.
    agg = reindexed.ffill()
    if bool(agg["last"].isna().any()):
        raise DataUnavailable(
            f"parity quotes do not cover the {symbol} {day} session open "
            f"(leading gap of {leading_missing} bars); refusing to back-fill the open "
            "from a future quote."
        )
    if cov.filled:
        logger.info(
            "parity %s %s: forward-filled %d/%d missing %s bars (coverage %.1f%%)",
            symbol, day, cov.filled, expected, interval, cov.coverage * 100,
        )
    out = pd.DataFrame(index=target)
    out["open"] = agg["first"].to_numpy()
    out["high"] = agg["max"].to_numpy()
    out["low"] = agg["min"].to_numpy()
    out["close"] = agg["last"].to_numpy()
    out["volume"] = 0.0
    out = out[list(BAR_COLUMNS)]
    out.index.name = "ts"
    return BarSeries(symbol.upper(), interval, out, DataSource.PARITY, latency)


# Quote source: (symbol, day) -> ATM call/put quote frame (operator wires over Theta).
QuoteSource = Callable[[str, date], pd.DataFrame]


class ParityUnderlyingProvider(DataProvider):
    """Underlying bars reconstructed from ATM option quotes via put-call parity.

    ``quote_source(symbol, day)`` returns the synchronized ATM call/put quote frame
    (see :func:`reconstruct_spot`). The operator wires it over Theta option quotes;
    tests use a fake. Option methods raise — parity *consumes* options to recover
    the underlying, it does not re-serve them.

    Dividend yield ``q`` defaults to 0.0. Since ``S = F·e^{−(r−q)T}``, leaving
    ``q=0`` biases the reconstructed spot LOW by ``≈ q·T·S`` (e.g. SPY ~1.3%/yr,
    QQQ ~0.6%/yr dividends). Pass a per-symbol ``q`` for an unbiased reconstruction;
    the bias is negligible for 0DTE (``T→0``) but matters at multi-week maturities.
    """

    source = DataSource.PARITY

    def __init__(
        self,
        quote_source: QuoteSource,
        *,
        config: EngineConfig | None = None,
        r: float | None = None,
        q: float = 0.0,
        min_coverage: float = 0.8,
    ) -> None:
        self.quote_source = quote_source
        self.config = config or EngineConfig.default()
        self.r = r if r is not None else self.config.gate.risk_free_rate
        self.q = q
        self.min_coverage = min_coverage

    def trading_days(self, start: date, end: date) -> list[date]:
        return _td(start, end)

    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        quotes = self.quote_source(symbol, day)
        if quotes is None or len(quotes) == 0:
            raise DataUnavailable(f"no parity quotes for {symbol} {day}")
        spot = reconstruct_spot(quotes, r=self.r, q=self.q, session=self.config.session)
        return spot_to_bars(
            spot, symbol, day, interval,
            session=self.config.session,
            latency=pd.Timedelta(milliseconds=self.config.data.bar_latency_ms),
            min_coverage=self.min_coverage,
        )

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        raise DataUnavailable(
            "ParityUnderlyingProvider reconstructs the UNDERLYING from option "
            "quotes; it does not serve option chains (use Theta)."
        )

    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        raise DataUnavailable(
            "ParityUnderlyingProvider reconstructs the UNDERLYING from option "
            "quotes; it does not serve the option tape (use Theta)."
        )
