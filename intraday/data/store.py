"""Partitioned parquet store for the intraday engine (DESIGN.md §2.3).

Implements the ``ticker=<SYM>/date=<YYYY-MM-DD>`` layout directly (this differs
from SWE's ``data.feature_store``, which partitions by ``ticker=`` only and pulls
in a heavy ``data/__init__`` — see ``docs/SWE_API_REFERENCE.md`` and PROGRESS.md
for the rationale). Layout::

    <root>/bars/        ticker=<SYM>/date=<D>/bars_<interval>.parquet
    <root>/option_tape/ ticker=<SYM>/date=<D>/trades.parquet
    <root>/option_chain/ticker=<SYM>/date=<D>/chain.parquet
    <root>/features/    ticker=<SYM>/date=<D>/<group>.parquet
    <root>/signals/     date=<D>/signals.parquet
    <root>/paper_ledger/date=<D>/fills.parquet

PIT discipline: bar frames are indexed by close ``ts``; tape/chain/feature frames
carry an ``available_ts`` column. The store persists frames losslessly (tz-aware
timestamps preserved by pyarrow) plus a small ``_meta.json`` sidecar per bar
partition so :class:`BarSeries` provenance (source, latency) round-trips.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from ..contracts import BarSeries, DataSource, OptionChainSeries, OptionTape

DEFAULT_STORE_ROOT = Path("data_store")


def _date_str(day: date) -> str:
    return pd.Timestamp(day).strftime("%Y-%m-%d")


class ParquetStore:
    """Read/write helpers for the partitioned parquet store."""

    def __init__(self, root: str | Path = DEFAULT_STORE_ROOT) -> None:
        self.root = Path(root)

    # -- path helpers --------------------------------------------------- #
    def _partition(self, group: str, symbol: str, day: date) -> Path:
        return self.root / group / f"ticker={symbol.upper()}" / f"date={_date_str(day)}"

    def _date_partition(self, group: str, day: date) -> Path:
        return self.root / group / f"date={_date_str(day)}"

    # -- bars ----------------------------------------------------------- #
    def write_bars(self, series: BarSeries, day: date) -> Path:
        part = self._partition("bars", series.symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / f"bars_{series.interval}.parquet"
        series.frame.to_parquet(path, engine="pyarrow", index=True)
        meta = {
            "symbol": series.symbol,
            "interval": series.interval,
            "source": series.source.value,
            "latency_ms": int(series.latency / pd.Timedelta(milliseconds=1)),
        }
        (part / f"bars_{series.interval}_meta.json").write_text(json.dumps(meta))
        return path

    def read_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        part = self._partition("bars", symbol, day)
        path = part / f"bars_{interval}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no bars at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        meta_path = part / f"bars_{interval}_meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            source = DataSource(meta["source"])
            latency = pd.Timedelta(milliseconds=meta["latency_ms"])
        else:  # pragma: no cover - sidecar always written by write_bars
            source, latency = DataSource.SYNTHETIC, pd.Timedelta(0)
        return BarSeries(symbol.upper(), interval, frame, source, latency)

    # -- option tape ---------------------------------------------------- #
    def write_tape(self, tape: OptionTape, day: date) -> Path:
        part = self._partition("option_tape", tape.symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / "trades.parquet"
        tape.frame.to_parquet(path, engine="pyarrow", index=False)
        return path

    def read_tape(self, symbol: str, day: date) -> OptionTape:
        path = self._partition("option_tape", symbol, day) / "trades.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no tape at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        return OptionTape(symbol.upper(), frame, DataSource.SYNTHETIC)

    # -- option chain --------------------------------------------------- #
    def write_chain(self, chain: OptionChainSeries, day: date) -> Path:
        part = self._partition("option_chain", chain.symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / "chain.parquet"
        chain.frame.to_parquet(path, engine="pyarrow", index=False)
        return path

    def read_chain(self, symbol: str, day: date) -> OptionChainSeries:
        path = self._partition("option_chain", symbol, day) / "chain.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no chain at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        return OptionChainSeries(symbol.upper(), frame, DataSource.SYNTHETIC)

    # -- features ------------------------------------------------------- #
    def write_features(self, symbol: str, day: date, group: str, df: pd.DataFrame) -> Path:
        part = self._partition("features", symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / f"{group}.parquet"
        df.to_parquet(path, engine="pyarrow", index=False)
        return path

    def read_features(self, symbol: str, day: date, group: str) -> pd.DataFrame:
        path = self._partition("features", symbol, day) / f"{group}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no features at {path}")
        return pd.read_parquet(path, engine="pyarrow")

    # -- signals / ledger (date-partitioned) ---------------------------- #
    def write_signals(self, day: date, df: pd.DataFrame) -> Path:
        part = self._date_partition("signals", day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / "signals.parquet"
        df.to_parquet(path, engine="pyarrow", index=False)
        return path

    def write_ledger(self, day: date, df: pd.DataFrame) -> Path:
        part = self._date_partition("paper_ledger", day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / "fills.parquet"
        df.to_parquet(path, engine="pyarrow", index=False)
        return path
