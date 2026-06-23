"""Reconstruct PIT option chains from SWE's deep index option history (offline).

``theta/index_reference/option_history/ticker=<SYM>/expiration=<YYYYMMDD>/`` holds
per-contract **daily EOD** rows (OHLC + NBBO bid/ask + ``open_interest``) for the
index ETFs/indices SPX/SPY/QQQ/NDX/RUT, 2016→2026. Unlike ``index_options_chains``
it carries **no implied-vol column**, so IV is Black-Scholes-inverted here from the
NBBO mid — at the SAME tenor the GEX consumer re-prices with (the dealer analyzer's
``max(days, 1)/365``), using a put-call-parity spot, the real risk-free rate, and a
per-symbol continuous dividend yield.

This is the bridge from the *deepest* real asset to a real multi-year GEX/VRP
series. It is intentionally a separate, well-tested module (not yet on the engine
hot path) so the IV-inversion correctness can be validated before any backtest
relies on it.

Honest scope:
- **SPY/QQQ only** by default (PM-settled, known dividend yield). SPX/SPXW are
  REFUSED (AM/PM settlement ambiguity + junk SPXW OI), mirroring
  :mod:`intraday.data.chain_synthesis`.
- A reconstruction is **refused (returns None)**, never approximated, when there
  is no usable parity pair or too few strikes inverted — absence is reported, not
  guessed.
- EOD snapshots: the intraday-decay distinction between the spot clock and the IV
  clock (``chain_synthesis`` R7) collapses at the close, so a single
  ``T = days/365`` is used for both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import CHAIN_COLUMNS, DataSource, OptionChainSeries
from ..logging_config import get_logger
from .parity import implied_spot
from .swe_offline import SweDataUnavailable, swe_root

logger = get_logger(__name__)

_HISTORY_DIR = ("data_processed", "theta", "index_reference", "option_history")

# PM-settled, known continuous dividend yield (mirrors chain_synthesis / parity).
PER_SYMBOL_Q: dict[str, float] = {"SPY": 0.013, "QQQ": 0.006}
REFUSED_ROOTS = frozenset({"SPX", "SPXW"})

# Junk-IV ceiling: a deep wing with ~zero vega can "converge" to an absurd root;
# the SWE dealer analyzer keeps iv < 5.0, so match that bound.
_MAX_PLAUSIBLE_IV = 5.0
# Reject a leg whose relative spread is implausibly wide (stale/illiquid quote).
_MAX_REL_SPREAD = 0.60


@dataclass(frozen=True)
class ReconStats:
    """Per-reconstruction accounting (for transparency / validation)."""

    symbol: str
    day: date
    expiry: date
    dte: int
    spot: float | None
    rows_in_snapshot: int
    rows_quoted: int          # bid>0 & ask>bid & sane spread
    rows_inverted: int        # IV successfully inverted (kept)
    parity_pairs: int


def list_history_expiries(symbol: str, root: str | None = None) -> list[tuple[date, Path]]:
    """``(expiry, partition_path)`` for every captured expiry of ``symbol`` (sorted)."""
    base = Path(root) if root else swe_root()
    d = base.joinpath(*_HISTORY_DIR) / f"ticker={symbol.upper()}"
    if not d.exists():
        raise SweDataUnavailable(f"no deep option history for {symbol!r} at {d}")
    out: list[tuple[date, Path]] = []
    for part in sorted(d.glob("expiration=*")):
        stamp = part.name.split("=", 1)[1]
        try:
            out.append((pd.Timestamp(stamp).date(), part))
        except ValueError:  # pragma: no cover - malformed partition
            continue
    if not out:
        raise SweDataUnavailable(f"no expiration partitions for {symbol!r} in {d}")
    return out


def _read_partition(path: Path) -> pd.DataFrame:
    files = list(path.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    frames = [pd.read_parquet(f) for f in files]
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    df["created_date"] = pd.to_datetime(df["created"]).dt.date
    return df


def _eod_snapshot(part: pd.DataFrame, day: date) -> pd.DataFrame:
    """The single EOD snapshot of one expiry partition on ``day`` (rows created
    that day). If several intraday prints exist, keep the last per contract."""
    rows = part.loc[part["created_date"] == day]
    if rows.empty:
        return rows
    # One row per (strike, right): the last created print of the day.
    rows = rows.sort_values("created")
    rows = rows.drop_duplicates(subset=["strike", "right"], keep="last")
    return rows


def _parity_spot(quoted: pd.DataFrame, *, r: float, q: float, T: float, n_pairs: int = 3):
    """Median put-call-parity spot over the ``n_pairs`` nearest-forward strikes.

    A strike contributes only if BOTH its call and put have a usable mid; the
    nearest-forward strikes are those with the smallest ``|call_mid - put_mid|``.
    Returns ``(spot|None, n_pairs_used)``.
    """
    piv = quoted.pivot_table(index="strike", columns="right", values="mid", aggfunc="last")
    if "CALL" not in piv.columns or "PUT" not in piv.columns:
        return None, 0
    pair = piv.dropna(subset=["CALL", "PUT"]).copy()
    if pair.empty:
        return None, 0
    pair["fwd_gap"] = (pair["CALL"] - pair["PUT"]).abs()
    near = pair.nsmallest(n_pairs, "fwd_gap")
    spots = [
        implied_spot(row["CALL"], row["PUT"], float(k), r=r, q=q, T=T)
        for k, row in near.iterrows()
    ]
    spots = [s for s in spots if s is not None and np.isfinite(s) and s > 0]
    if not spots:
        return None, 0
    return float(np.median(spots)), len(spots)


def reconstruct_chain(
    symbol: str,
    day: date,
    expiry: date,
    *,
    r: float,
    q: float | None = None,
    root: str | None = None,
    min_strikes: int = 8,
    strike_window_pct: float | None = None,
    latency: pd.Timedelta = pd.Timedelta(seconds=1),
    _partition: pd.DataFrame | None = None,
) -> tuple[OptionChainSeries, ReconStats] | None:
    """Reconstruct the EOD option chain for ``(symbol, day, expiry)`` with inverted IV.

    Returns ``(OptionChainSeries, ReconStats)`` or ``None`` when the snapshot is
    missing, has no usable parity spot, or yields fewer than ``min_strikes``
    invertible contracts. ``r`` is the real risk-free rate for ``day``; ``q``
    defaults to the per-symbol dividend yield. ``_partition`` lets a caller pass an
    already-loaded expiry partition (avoids re-reading it across many days).
    """
    from engine.option_pricer import implied_volatility  # lazy (SWE)

    sym = symbol.upper()
    if sym in REFUSED_ROOTS:
        raise SweDataUnavailable(
            f"{sym} reconstruction refused (AM/PM settlement ambiguity); use SPY/QQQ"
        )
    if q is None:
        q = PER_SYMBOL_Q.get(sym, 0.0)

    if _partition is None:
        expiries = {e: p for e, p in list_history_expiries(sym, root)}
        if expiry not in expiries:
            return None
        _partition = _read_partition(expiries[expiry])
    snap = _eod_snapshot(_partition, day)
    if snap.empty:
        return None

    dte = (pd.Timestamp(expiry) - pd.Timestamp(day)).days
    if dte < 0:
        return None
    T = max(dte, 1) / 365.0

    q_ = snap.copy()
    q_["right"] = q_["right"].astype(str).str.upper()
    q_ = q_.loc[(q_["bid"] > 0) & (q_["ask"] > q_["bid"])]
    if q_.empty:
        return None
    q_["mid"] = 0.5 * (q_["bid"] + q_["ask"])
    # Reject implausibly wide (stale) quotes.
    q_ = q_.loc[(q_["ask"] - q_["bid"]) <= _MAX_REL_SPREAD * q_["mid"].clip(lower=1e-6) * 2]

    spot, n_pairs = _parity_spot(q_, r=r, q=q, T=T)
    if spot is None:
        return None

    # Optionally invert only the gamma-relevant strikes near spot. Deep wings carry
    # ~zero gamma, so restricting the (expensive) IV inversion to a ±window of spot
    # is a large speedup for a multi-year sweep with negligible effect on GEX. The
    # OI of dropped wings is excluded too — acceptable since their gamma weight ~0.
    if strike_window_pct is not None:
        lo, hi = spot * (1 - strike_window_pct), spot * (1 + strike_window_pct)
        q_ = q_.loc[(q_["strike"] >= lo) & (q_["strike"] <= hi)]
        if q_.empty:
            return None

    ivs: list[float] = []
    keep_idx: list[int] = []
    for idx, row in q_.iterrows():
        otype = "call" if row["right"] == "CALL" else "put"
        iv = implied_volatility(
            float(row["mid"]), spot, float(row["strike"]), T, r, otype, q=q
        )
        if iv is None or not np.isfinite(iv) or iv <= 0 or iv >= _MAX_PLAUSIBLE_IV:
            continue
        ivs.append(float(iv))
        keep_idx.append(idx)

    if len(keep_idx) < min_strikes:
        return None
    kept = q_.loc[keep_idx]

    snap_ts = (
        pd.Timestamp(day).tz_localize("America/New_York").replace(hour=16, minute=0)
        .tz_convert("UTC")
    )
    frame = pd.DataFrame(
        {
            "snapshot_ts": snap_ts,
            "available_ts": snap_ts + latency,
            "expiration": pd.Timestamp(expiry),
            "strike": kept["strike"].astype(float).to_numpy(),
            "option_type": np.where(kept["right"].to_numpy() == "CALL", "call", "put"),
            "open_interest": kept["open_interest"].fillna(0).astype("int64").to_numpy(),
            "implied_vol": np.asarray(ivs, dtype=float),
            "spot": float(spot),
        }
    )[list(CHAIN_COLUMNS)]

    stats = ReconStats(
        symbol=sym, day=day, expiry=expiry, dte=int(dte), spot=float(spot),
        rows_in_snapshot=int(len(snap)), rows_quoted=int(len(q_)),
        rows_inverted=int(len(keep_idx)), parity_pairs=int(n_pairs),
    )
    return OptionChainSeries(sym, frame, DataSource.THETA), stats
