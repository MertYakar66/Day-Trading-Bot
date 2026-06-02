"""Deterministic SYNTHETIC intraday data provider (the Phase-0 workhorse).

Why synthetic: the Theta Terminal is off-limits this session (the operator uses
the subscription concurrently) and the tier is FREE (no real intraday data
anyway). Every value here is generated from a seeded RNG and labelled
``DataSource.SYNTHETIC`` so it can never be mistaken for real market data.

Realism / internal consistency: a single canonical 1-second underlying price path
is generated per ``(symbol, day)``; bars, option-chain snapshots, and the option
tape are all derived from it, so the chain ``spot`` matches the bar close and the
tape prints track the underlying. The path is a slow random-walk "fair value"
plus a persistent mean-reverting (Ornstein-Uhlenbeck) deviation, which gives the
VWAP-reversion structure the S3 control strategy needs without baking in a
look-ahead-able edge.

This module is intentionally free of any SWE / network import so the data layer
is testable in isolation.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..config import DataConfig, SessionConfig
from ..contracts import (
    BAR_COLUMNS,
    CHAIN_COLUMNS,
    TAPE_COLUMNS,
    AssetKind,
    BarSeries,
    DataSource,
    OptionChainSeries,
    OptionTape,
)
from ..logging_config import get_logger
from ..timeutils import (
    bar_close_index,
    parse_interval,
    session_bounds_utc,
    stable_seed,
    trading_days as _trading_days,
)
from .provider import DataProvider, asset_kind_for

logger = get_logger(__name__)

# Synthetic price anchors (clearly fictional but order-of-magnitude plausible).
_ANCHOR: dict[str, float] = {"SPX": 6000.0, "SPY": 755.0, "QQQ": 540.0, "VIX": 15.0}
_DAILY_SIGMA: dict[AssetKind, float] = {
    AssetKind.STOCK: 0.012,
    AssetKind.INDEX: 0.010,
    AssetKind.OPTION: 0.012,
}
_BASE_VOL_PER_SEC: dict[str, float] = {"SPY": 1500.0, "QQQ": 800.0}
_STRIKE_SPACING: dict[str, float] = {"SPX": 25.0, "SPY": 5.0, "QQQ": 5.0}

_ATM_IV: dict[str, float] = {"SPX": 0.13, "SPY": 0.13, "QQQ": 0.16}
# Default path shape lives in DataConfig (fv_weight/dev_weight/ou_phi). It is
# random-walk DOMINANT, matching the near-zero minute-bar return autocorrelation
# of real liquid index ETFs. An earlier strongly-reverting OU made a naive
# VWAP-reversion control look fabulous (Sharpe ~14) — a synthetic artifact, not
# an edge. The shape is a realism choice, set independently of any strategy's PnL.
# See PROGRESS.md.
_CHAIN_SNAPSHOT_MIN = 5  # option-chain snapshot cadence
_N_STRIKES = 10          # ATM ± N (DESIGN §2.2)
_TAPE_PRINTS_PER_DAY = 600


class SyntheticDataProvider(DataProvider):
    """Seeded, reproducible synthetic intraday data for SPX/SPY/QQQ."""

    source = DataSource.SYNTHETIC

    def __init__(
        self,
        data: DataConfig | None = None,
        session: SessionConfig | None = None,
    ) -> None:
        self.data = data or DataConfig()
        self.session = session or SessionConfig()
        self._path_cache: dict[tuple[str, date], pd.Series] = {}

    # ------------------------------------------------------------------ #
    # DataProvider interface
    # ------------------------------------------------------------------ #
    def trading_days(self, start: date, end: date) -> list[date]:
        return _trading_days(start, end)

    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        path = self._underlying_path(symbol, day)
        frame = self._bars_from_path(symbol, day, interval, path)
        latency = pd.Timedelta(milliseconds=self.data.bar_latency_ms)
        return BarSeries(symbol.upper(), interval, frame, self.source, latency)

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        frame = self._build_chain(symbol, day)
        return OptionChainSeries(symbol.upper(), frame, self.source)

    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        frame = self._build_tape(symbol, day)
        return OptionTape(symbol.upper(), frame, self.source)

    # ------------------------------------------------------------------ #
    # Canonical underlying path (1-second), cached per (symbol, day)
    # ------------------------------------------------------------------ #
    def _underlying_path(self, symbol: str, day: date) -> pd.Series:
        symbol = symbol.upper()
        key = (symbol, day)
        cached = self._path_cache.get(key)
        if cached is not None:
            return cached

        open_utc, close_utc = session_bounds_utc(day, self.session)
        idx = pd.date_range(start=open_utc + pd.Timedelta(seconds=1), end=close_utc, freq="1s")
        n = len(idx)
        kind = asset_kind_for(symbol)
        base = _ANCHOR.get(symbol, 100.0)
        daily_sigma = _DAILY_SIGMA.get(kind, 0.012)
        sec_sigma = daily_sigma / np.sqrt(n)

        rng = np.random.default_rng(stable_seed(self.data.rng_seed, symbol, day))
        # Dominant "fair value" random walk (efficient-price component).
        fv = np.cumsum(rng.normal(0.0, self.data.fv_weight * sec_sigma, n))
        # Mean-reverting deviation around fair value (AR(1)).
        eps = rng.normal(0.0, self.data.dev_weight * sec_sigma, n)
        dev = np.empty(n)
        prev = 0.0
        phi = self.data.ou_phi
        for i in range(n):
            prev = phi * prev + eps[i]
            dev[i] = prev
        price = base * np.exp(fv + dev)
        series = pd.Series(price, index=idx, name=symbol)
        self._path_cache[key] = series
        return series

    # ------------------------------------------------------------------ #
    # Bars
    # ------------------------------------------------------------------ #
    def _bars_from_path(
        self, symbol: str, day: date, interval: str, path: pd.Series
    ) -> pd.DataFrame:
        symbol = symbol.upper()
        step = parse_interval(interval)
        agg = (
            path.resample(step, label="right", closed="right")
            .agg(["first", "max", "min", "last"])
        )
        target = bar_close_index(day, interval, self.session)
        agg = agg.reindex(target)
        agg = agg.ffill().bfill()
        out = pd.DataFrame(index=target)
        out["open"] = agg["first"].to_numpy()
        out["close"] = agg["last"].to_numpy()
        out["high"] = agg["max"].to_numpy()
        out["low"] = agg["min"].to_numpy()

        # Volume: U-shaped intraday profile (index instruments carry none).
        kind = asset_kind_for(symbol)
        if kind is AssetKind.INDEX:
            out["volume"] = 0.0
        else:
            rng = np.random.default_rng(
                stable_seed(self.data.rng_seed + 17, symbol + "|vol|" + interval, day)
            )
            m = len(target)
            t = np.linspace(0.0, 1.0, m)
            u = 1.0 + 2.0 * (np.exp(-t / 0.12) + np.exp(-(1.0 - t) / 0.12))
            base_v = _BASE_VOL_PER_SEC.get(symbol, 500.0) * step.total_seconds()
            noise = rng.uniform(0.7, 1.3, m)
            out["volume"] = np.round(base_v * u * noise).astype(float)

        out = out[list(BAR_COLUMNS)]
        out.index.name = "ts"
        return out

    # ------------------------------------------------------------------ #
    # Option chain snapshots
    # ------------------------------------------------------------------ #
    def _build_chain(self, symbol: str, day: date) -> pd.DataFrame:
        symbol = symbol.upper()
        path = self._underlying_path(symbol, day)
        open_utc, close_utc = session_bounds_utc(day, self.session)
        snap_times = pd.date_range(
            start=open_utc, end=close_utc, freq=f"{_CHAIN_SNAPSHOT_MIN}min"
        )
        spacing = _STRIKE_SPACING.get(symbol, 5.0)
        atm_iv = _ATM_IV.get(symbol, 0.15)
        latency = pd.Timedelta(milliseconds=self.data.chain_latency_ms)
        rng = np.random.default_rng(
            stable_seed(self.data.rng_seed + 101, symbol + "|chain", day)
        )

        rows: list[dict] = []
        for snap_ts in snap_times:
            # PIT-safe spot: the last canonical price at/before the snapshot.
            pos = path.index.searchsorted(snap_ts, side="right") - 1
            if pos < 0:
                spot = float(path.iloc[0])
            else:
                spot = float(path.iloc[pos])
            atm_strike = round(spot / spacing) * spacing
            strikes = atm_strike + spacing * np.arange(-_N_STRIKES, _N_STRIKES + 1)
            # Gamma walls: call OI peaks above spot, put OI peaks below; round
            # strikes (multiples of 5×spacing) get an OI boost → distinct walls.
            for k in strikes:
                moneyness = (k - spot) / spot
                # Volatility smile/skew (puts richer).
                iv = atm_iv * (1.0 + 1.2 * max(0.0, -moneyness) + 4.0 * moneyness**2)
                iv = float(np.clip(iv, 0.05, 1.5))
                round_boost = 2.0 if abs(k % (5 * spacing)) < 1e-6 else 1.0
                call_center = spot * 1.01
                put_center = spot * 0.99
                call_oi = 4000.0 * np.exp(-((k - call_center) / (spot * 0.02)) ** 2)
                put_oi = 4000.0 * np.exp(-((k - put_center) / (spot * 0.02)) ** 2)
                call_oi *= round_boost * rng.uniform(0.85, 1.15)
                put_oi *= round_boost * rng.uniform(0.85, 1.15)
                rows.append(
                    {
                        "snapshot_ts": snap_ts,
                        "available_ts": snap_ts + latency,
                        "expiration": day,  # 0DTE near-dated (DESIGN §2.2)
                        "strike": float(k),
                        "option_type": "C",
                        "open_interest": int(max(0, round(call_oi))),
                        "implied_vol": iv,
                        "spot": spot,
                    }
                )
                rows.append(
                    {
                        "snapshot_ts": snap_ts,
                        "available_ts": snap_ts + latency,
                        "expiration": day,
                        "strike": float(k),
                        "option_type": "P",
                        "open_interest": int(max(0, round(put_oi))),
                        "implied_vol": iv,
                        "spot": spot,
                    }
                )
        frame = pd.DataFrame(rows, columns=list(CHAIN_COLUMNS))
        return frame

    # ------------------------------------------------------------------ #
    # Option tape (trade-by-trade, with NBBO + side_inferred)
    # ------------------------------------------------------------------ #
    def _build_tape(self, symbol: str, day: date) -> pd.DataFrame:
        symbol = symbol.upper()
        path = self._underlying_path(symbol, day)
        spacing = _STRIKE_SPACING.get(symbol, 5.0)
        atm_iv = _ATM_IV.get(symbol, 0.15)
        latency = pd.Timedelta(milliseconds=self.data.tape_latency_ms)
        rng = np.random.default_rng(
            stable_seed(self.data.rng_seed + 211, symbol + "|tape", day)
        )

        n_prints = _TAPE_PRINTS_PER_DAY
        sec_index = path.index
        n_sec = len(sec_index)
        # Random print times across the session, sorted.
        offsets = np.sort(rng.integers(0, n_sec, n_prints))
        # Short-horizon momentum of the underlying drives buy/sell pressure.
        logp = np.log(path.to_numpy())
        lookback = 60  # seconds
        rows: list[dict] = []
        for off in offsets:
            ts = sec_index[off]
            spot = float(path.iloc[off])
            prev = logp[max(0, off - lookback)]
            momentum = logp[off] - prev
            # Probability of a buy-initiated print rises with recent up-moves.
            p_buy = 1.0 / (1.0 + np.exp(-momentum / 0.0008))
            right = "C" if rng.random() < 0.5 else "P"
            # ATM-ish strike.
            k = round(spot / spacing) * spacing + spacing * rng.integers(-2, 3)
            moneyness = (k - spot) / spot
            # Crude option mid: intrinsic + time value bump near ATM.
            intrinsic = max(0.0, spot - k) if right == "C" else max(0.0, k - spot)
            tv = atm_iv * spot * 0.02 * np.exp(-((moneyness) / 0.01) ** 2)
            mid = max(0.05, intrinsic + tv)
            half_spread = max(0.02, mid * 0.02)
            bid = mid - half_spread
            ask = mid + half_spread
            is_buy = rng.random() < p_buy
            if is_buy:
                price = ask
                side = "buy"
            else:
                price = bid
                side = "sell"
            rows.append(
                {
                    "ts": ts,
                    "available_ts": ts + latency,
                    "expiration": day,
                    "strike": float(k),
                    "right": right,
                    "price": float(round(price, 2)),
                    "size": int(rng.integers(1, 50)),
                    "nbbo_bid": float(round(bid, 2)),
                    "nbbo_ask": float(round(ask, 2)),
                    "side_inferred": side,
                }
            )
        frame = pd.DataFrame(rows, columns=list(TAPE_COLUMNS))
        return frame
