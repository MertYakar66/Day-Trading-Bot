"""Offline bridge to the Smart-Wheel-Engine (SWE) on-disk datasets — NETWORK-FREE.

This module reads SWE's *captured* Bloomberg / Theta / vol-index files DIRECTLY
FROM DISK. It never opens a socket: no Theta Terminal (``127.0.0.1:25503``), no
Bloomberg API, no HTTP. The autouse ``_block_network`` test fixture stays green
while these loaders run — that green suite is the proof the path is offline.

It is the real-data counterpart to :mod:`intraday.data.synthetic`: where the
synthetic provider fabricates a clearly-labelled world, this bridge surfaces the
operator's already-captured real data, **with every documented defect corrected**
so the engine consumes clean, PIT-safe inputs. The corrections (verified by the
real-data test workstreams; see ``docs/SWE_DATA_DEFECTS.md``):

- ``sp500_vol_iv_full.csv`` is the CORRECT implied-vol source. ``hist_put_imp_vol``
  / ``hist_call_imp_vol`` are in **percent** (divide by 100). The mislabeled
  ``iv_atm`` in ``theta/iv_history`` (which is actually 30-day *realized* vol) is
  NEVER used here. The file ends 2026-03-20, a partial-day triple-witching
  snapshot — that final row is dropped (never used as a close).
- ``treasury_yields.csv`` gives the real risk-free curve (decimals), replacing the
  engine's hardcoded ``GateConfig.risk_free_rate = 0.04``.
- ``vol_indices_wide.parquet`` gives the daily VIX complex (term structure,
  vol-of-vol, skew) for a real vol-regime context layer.
- ``theta/index_options_chains/<SYM>_<YYYYMMDD>.parquet`` are real index option
  chains (SPX/NDX/RUT/VIX/...) with real greeks, IV and open interest. They carry
  the *real* expiration, so GEX must price at that expiry (NOT the engine's
  hardcoded same-day expiry, which inflated real multi-week GEX).

Every loader RAISES on missing/empty data rather than guessing — consistent with
the store's "never silently relabel / never assume" honesty guardrail.

PIT discipline: a daily series stamped at a session close on date ``d`` first
becomes usable for a decision on the *next* session. The ``*_asof`` helpers
default to a strictly-prior-session lookup so a same-day close can never leak into
an intraday decision on that same day.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from ..contracts import CHAIN_COLUMNS, DataSource, OptionChainSeries
from ..logging_config import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Locating the SWE repo (read-only; default matches the operator's checkout).
# --------------------------------------------------------------------------- #
_DEFAULT_SWE_ROOT = Path(r"C:/Users/merty/Desktop/smart-wheel-engine")

# The last contiguous "clean close" we trust from the Bloomberg pulls: the
# 2026-03-20 row is a ~15:00 ET triple-witching partial-day snapshot, not a close.
PARTIAL_DAY_TRAP = pd.Timestamp("2026-03-20")


def swe_root() -> Path:
    """The SWE repo root. Override with ``$SWE_ROOT`` (used by tests/fixtures)."""
    env = os.environ.get("SWE_ROOT")
    return Path(env) if env else _DEFAULT_SWE_ROOT


class SweDataUnavailable(RuntimeError):
    """Requested SWE on-disk data is missing, empty, or unusable after cleaning."""


def _require(path: Path) -> Path:
    if not path.exists():
        raise SweDataUnavailable(
            f"SWE file not found: {path}. Set $SWE_ROOT or pass root= explicitly; "
            "this bridge only reads captured files (it never connects to a feed)."
        )
    return path


# --------------------------------------------------------------------------- #
# 1) Risk-free curve (treasury_yields.csv) — decimals, PIT lookup.
# --------------------------------------------------------------------------- #
_TENORS = ("rate_3m", "rate_6m", "rate_2y", "rate_10y")


@dataclass(frozen=True)
class RiskFreeCurve:
    """Daily constant-maturity treasury yields (annualized decimals).

    ``frame`` is indexed by date (tz-naive midnight) with columns
    :data:`_TENORS`. :meth:`rate_on` is PIT: it returns the most recent rate at or
    before ``day`` (strictly before, when ``prior_session=True``), so a same-day
    print cannot leak forward. Short-dated options (0DTE..weeks) use ``rate_3m``.
    """

    frame: pd.DataFrame

    def rate_on(
        self, day: date, tenor: str = "rate_3m", *, prior_session: bool = True
    ) -> float:
        if tenor not in self.frame.columns:
            raise SweDataUnavailable(f"unknown tenor {tenor!r}; have {_TENORS}")
        ts = pd.Timestamp(day).normalize()
        cutoff = ts - pd.Timedelta(days=1) if prior_session else ts
        avail = self.frame.loc[self.frame.index <= cutoff, tenor].dropna()
        if avail.empty:
            raise SweDataUnavailable(
                f"no treasury {tenor} on/before {cutoff.date()} "
                f"(curve starts {self.frame.index.min().date()})"
            )
        return float(avail.iloc[-1])

    @property
    def start(self) -> date:
        return self.frame.index.min().date()

    @property
    def end(self) -> date:
        return self.frame.index.max().date()


@lru_cache(maxsize=8)
def load_risk_free_curve(root: str | None = None) -> RiskFreeCurve:
    """Load the treasury yield curve from SWE Bloomberg ``treasury_yields.csv``."""
    base = Path(root) if root else swe_root()
    path = _require(base / "data" / "bloomberg" / "treasury_yields.csv")
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    keep = [c for c in _TENORS if c in df.columns]
    if not keep:
        raise SweDataUnavailable(f"{path} has no recognised tenor columns")
    df = df[keep].astype(float)
    # The Bloomberg treasury file is in PERCENT (rate_10y ~ 4.4 means 4.4%); the
    # engine wants annualized DECIMALS (0.044). Convert, then sanity-check the
    # result is plausibly a decimal rate — a >100% post-conversion rate would mean
    # the file changed units and we'd be feeding the pricer a 100x error.
    df = df / 100.0
    rate_max = float(df.to_numpy().max())
    if rate_max > 1.0:
        raise SweDataUnavailable(
            f"{path} rates implausible after %->decimal (max {rate_max:.2f} > 1.0); "
            "refusing to feed the pricer a mis-scaled rate"
        )
    return RiskFreeCurve(df)


# --------------------------------------------------------------------------- #
# 2) Vol-regime panel (vol_indices_wide.parquet) — daily VIX complex.
# --------------------------------------------------------------------------- #
# Map the on-disk ``*_close`` columns to terse feature names (points, not decimals:
# vix==16.7 means 16.7% annualized).
_VOLIDX_RENAME = {
    "vix_close": "vix",
    "vix3m_close": "vix3m",
    "vix6m_close": "vix6m",
    "vix9d_close": "vix9d",
    "vvix_close": "vvix",
    "skew_close": "skew",
    "move_close": "move",
    "ovx_close": "ovx",
    "gvz_close": "gvz",
    "vxn_close": "vxn",
}


@dataclass(frozen=True)
class VolRegimePanel:
    """Daily volatility-complex closes for a real vol-regime context layer.

    Columns (points): ``vix vix3m vix6m vix9d vvix skew move ovx gvz vxn``.
    Derived, PIT-safe (prior-session) helpers:

    - :meth:`term_slope` — ``vix / vix3m``. < 1 ⇒ contango (calm, vol cheap at the
      front); > 1 ⇒ backwardation (stress, front vol bid). The single most useful
      equity vol-regime gauge.
    - :meth:`front_slope` — ``vix9d / vix``. The very front of the curve (0DTE-ish
      stress).
    - :meth:`vvix`, :meth:`skew` — vol-of-vol and tail-risk pricing.
    """

    frame: pd.DataFrame

    def row_asof(self, day: date, *, prior_session: bool = True) -> pd.Series | None:
        ts = pd.Timestamp(day).normalize()
        cutoff = ts - pd.Timedelta(days=1) if prior_session else ts
        avail = self.frame.loc[self.frame.index <= cutoff]
        if avail.empty:
            return None
        return avail.iloc[-1]

    def _val(self, day: date, col: str, *, prior_session: bool = True) -> float | None:
        row = self.row_asof(day, prior_session=prior_session)
        if row is None or col not in row or pd.isna(row[col]):
            return None
        return float(row[col])

    def term_slope(self, day: date, *, prior_session: bool = True) -> float | None:
        vix = self._val(day, "vix", prior_session=prior_session)
        vix3m = self._val(day, "vix3m", prior_session=prior_session)
        if vix is None or not vix3m:
            return None
        return vix / vix3m

    def front_slope(self, day: date, *, prior_session: bool = True) -> float | None:
        vix9d = self._val(day, "vix9d", prior_session=prior_session)
        vix = self._val(day, "vix", prior_session=prior_session)
        if vix9d is None or not vix:
            return None
        return vix9d / vix

    def vvix(self, day: date, *, prior_session: bool = True) -> float | None:
        return self._val(day, "vvix", prior_session=prior_session)

    def skew(self, day: date, *, prior_session: bool = True) -> float | None:
        return self._val(day, "skew", prior_session=prior_session)

    def vix(self, day: date, *, prior_session: bool = True) -> float | None:
        return self._val(day, "vix", prior_session=prior_session)


@lru_cache(maxsize=8)
def load_vol_regime(root: str | None = None) -> VolRegimePanel:
    """Load the daily VIX complex from SWE ``vol_indices_wide.parquet``."""
    base = Path(root) if root else swe_root()
    path = _require(base / "data_processed" / "vol_indices_wide.parquet")
    df = pd.read_parquet(path)
    if "date" not in df.columns:
        raise SweDataUnavailable(f"{path} lacks a 'date' column")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    rename = {k: v for k, v in _VOLIDX_RENAME.items() if k in df.columns}
    df = df.rename(columns=rename)[list(rename.values())].astype(float)
    return VolRegimePanel(df)


# --------------------------------------------------------------------------- #
# 3) Daily implied-vol history (sp500_vol_iv_full.csv) — CORRECT IV source.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IVHistory:
    """Daily ATM implied + realized vol for one underlying (annualized decimals).

    Columns: ``atm_iv`` (mean of put/call implied), ``call_iv``, ``put_iv``,
    ``rv_30d`` ``rv_60d`` ``rv_90d`` ``rv_260d`` (Bloomberg realized vols). All
    converted from the on-disk PERCENT to decimals. The 2026-03-20 partial-day row
    is excluded. Indexed by date.
    """

    ticker: str
    frame: pd.DataFrame

    def asof(self, day: date, *, prior_session: bool = True) -> pd.Series | None:
        ts = pd.Timestamp(day).normalize()
        cutoff = ts - pd.Timedelta(days=1) if prior_session else ts
        avail = self.frame.loc[self.frame.index <= cutoff]
        if avail.empty:
            return None
        return avail.iloc[-1]

    def atm_iv_asof(self, day: date, *, prior_session: bool = True) -> float | None:
        row = self.asof(day, prior_session=prior_session)
        if row is None or pd.isna(row["atm_iv"]):
            return None
        return float(row["atm_iv"])


# Single-name roots in the Yahoo universe that exist in vol_iv_full (verified).
# Index ETFs/indices (SPY/QQQ/SPX/NDX) are ABSENT from this file by construction.
def _bloomberg_root(ticker: str) -> str:
    """Normalise to the equity root (drop any Bloomberg exchange suffix)."""
    return ticker.strip().upper().split(" ")[0]


@lru_cache(maxsize=1)
def _load_vol_iv_full(root_str: str) -> pd.DataFrame:
    base = Path(root_str)
    path = _require(base / "data" / "bloomberg" / "sp500_vol_iv_full.csv")
    cols = [
        "date", "ticker", "hist_put_imp_vol", "hist_call_imp_vol",
        "volatility_30d", "volatility_60d", "volatility_90d", "volatility_260d",
    ]
    df = pd.read_csv(path, usecols=lambda c: c in cols)
    df["date"] = pd.to_datetime(df["date"])
    df["root"] = df["ticker"].map(_bloomberg_root)
    return df


def load_iv_history(ticker: str, root: str | None = None) -> IVHistory:
    """Daily ATM IV + realized vol history for ``ticker`` (e.g. ``"AAPL"``).

    Raises :class:`SweDataUnavailable` for an index/ETF symbol (SPY/QQQ/SPX) or
    any root not present — these have no Bloomberg single-name IV history.
    """
    base = Path(root) if root else swe_root()
    df = _load_vol_iv_full(str(base))
    want = _bloomberg_root(ticker)
    sub = df.loc[df["root"] == want].copy()
    if sub.empty:
        raise SweDataUnavailable(
            f"no Bloomberg IV history for {ticker!r} (root {want!r}). Index/ETF "
            "symbols (SPY/QQQ/SPX/NDX) are absent from sp500_vol_iv_full by "
            "construction; only S&P 500 single names are covered."
        )
    # Drop the 2026-03-20 partial-day trap and rows with no implied vols.
    sub = sub.loc[sub["date"] != PARTIAL_DAY_TRAP]
    sub = sub.dropna(subset=["hist_put_imp_vol", "hist_call_imp_vol"], how="all")
    sub = sub.set_index("date").sort_index()
    out = pd.DataFrame(index=sub.index)
    # PERCENT -> decimal.
    out["call_iv"] = sub["hist_call_imp_vol"] / 100.0
    out["put_iv"] = sub["hist_put_imp_vol"] / 100.0
    out["atm_iv"] = out[["call_iv", "put_iv"]].mean(axis=1)
    for n in (30, 60, 90, 260):
        col = f"volatility_{n}d"
        if col in sub.columns:
            out[f"rv_{n}d"] = sub[col] / 100.0
    out = out.dropna(subset=["atm_iv"])
    if out.empty:
        raise SweDataUnavailable(f"IV history for {ticker!r} is empty after cleaning")
    return IVHistory(want, out)


@lru_cache(maxsize=8)
def list_iv_history_roots(root: str | None = None) -> tuple[str, ...]:
    """All single-name roots present in the Bloomberg IV history."""
    base = Path(root) if root else swe_root()
    df = _load_vol_iv_full(str(base))
    return tuple(sorted(df["root"].unique()))


# --------------------------------------------------------------------------- #
# 4) Real index option chains (theta/index_options_chains) — real GEX input.
# --------------------------------------------------------------------------- #
_INDEX_CHAIN_DIR = ("data_processed", "theta", "index_options_chains")


def list_index_chain_snapshots(
    symbol: str, root: str | None = None
) -> list[tuple[date, Path]]:
    """``(snapshot_date, path)`` for every captured chain of ``symbol`` (sorted)."""
    base = Path(root) if root else swe_root()
    d = base.joinpath(*_INDEX_CHAIN_DIR)
    if not d.exists():
        raise SweDataUnavailable(f"index chain dir not found: {d}")
    out: list[tuple[date, Path]] = []
    for p in sorted(d.glob(f"{symbol.upper()}_*.parquet")):
        stamp = p.stem.split("_")[-1]
        try:
            out.append((pd.Timestamp(stamp).date(), p))
        except ValueError:  # pragma: no cover - malformed name
            continue
    if not out:
        raise SweDataUnavailable(
            f"no captured index chains for {symbol!r} in {d} "
            f"(have e.g. SPX/NDX/RUT/VIX/DJX/XSP)"
        )
    return out


def load_index_chain(
    symbol: str,
    snapshot_date: date,
    *,
    root: str | None = None,
    expiry: date | None = None,
    min_oi: int = 0,
    latency: pd.Timedelta = pd.Timedelta(seconds=1),
) -> tuple[OptionChainSeries, date]:
    """A real, defect-corrected single-snapshot index chain for GEX.

    Returns ``(OptionChainSeries, expiry)`` where the frame holds
    :data:`CHAIN_COLUMNS`. Corrections applied:

    - **One expiry only** (avoid the SPX+SPXW / multi-expiry GEX-blending defect).
      Defaults to the *nearest* expiry in the snapshot; pass ``expiry`` to pick one.
    - ``implied_vol`` kept only where ``iv > 0`` (deep wings quote iv==0); rows with
      non-positive OI optionally dropped via ``min_oi``.
    - ``spot`` taken from the real ``underlying_price``.

    The returned ``expiry`` MUST be passed to :func:`features.gex.gamma_structure_at`
    so the dealer analyzer prices greeks at the real tenor (the engine's hardcoded
    same-day expiry would misprice a multi-week chain).
    """
    snaps = {d: p for d, p in list_index_chain_snapshots(symbol, root)}
    if snapshot_date not in snaps:
        raise SweDataUnavailable(
            f"no {symbol} chain on {snapshot_date} (have {sorted(snaps)})"
        )
    raw = pd.read_parquet(snaps[snapshot_date])
    raw = raw.copy()
    # The captured index chains are heterogeneous: most (SPX/VIX/RUT/SPXW/XSP/DJX)
    # are greek chains with iv + OI + underlying_price; a few (e.g. NDX) are raw
    # quote snapshots with no iv/OI and are NOT usable for GEX. Validate up front
    # and refuse with a clear message rather than dying on a downstream KeyError.
    required = ("expiration", "strike", "right", "iv", "open_interest", "underlying_price")
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise SweDataUnavailable(
            f"{symbol} {snapshot_date} chain is not GEX-ready: missing {missing} "
            f"(have {list(raw.columns)[:12]}...). This snapshot is likely a raw "
            "quote capture, not a greek chain."
        )
    raw["expiration"] = pd.to_datetime(raw["expiration"])
    expiries = sorted(raw["expiration"].dt.date.unique())
    if expiry is None:
        expiry = expiries[0]  # nearest
    elif expiry not in expiries:
        raise SweDataUnavailable(
            f"{symbol} {snapshot_date} has no expiry {expiry} (have {expiries})"
        )
    chain = raw.loc[raw["expiration"].dt.date == expiry].copy()
    chain = chain.loc[chain["iv"] > 0.0]
    if min_oi > 0:
        chain = chain.loc[chain["open_interest"] >= min_oi]
    if chain.empty:
        raise SweDataUnavailable(
            f"{symbol} {snapshot_date} exp {expiry} empty after iv>0/min_oi filter"
        )
    # Snapshot timestamp: the EOD capture. We anchor at the US session close (16:00
    # ET) of the snapshot date and stamp availability with arrival latency.
    snap_ts = pd.Timestamp(snapshot_date).tz_localize("America/New_York").replace(
        hour=16, minute=0
    ).tz_convert("UTC")
    out = pd.DataFrame(
        {
            "snapshot_ts": snap_ts,
            "available_ts": snap_ts + latency,
            "expiration": pd.Timestamp(expiry),
            "strike": chain["strike"].astype(float).to_numpy(),
            "option_type": chain["right"].astype(str).str.lower().to_numpy(),
            "open_interest": chain["open_interest"].astype("int64").to_numpy(),
            "implied_vol": chain["iv"].astype(float).to_numpy(),
            "spot": float(chain["underlying_price"].iloc[0]),
        }
    )
    out = out[list(CHAIN_COLUMNS)]
    return OptionChainSeries(symbol.upper(), out, DataSource.THETA), expiry
