"""Partitioned parquet store for the intraday engine (DESIGN.md §2.3).

Implements the ``ticker=<SYM>/date=<YYYY-MM-DD>`` layout directly (this differs
from SWE's ``data.feature_store``, which partitions by ``ticker=`` only and pulls
in a heavy ``data/__init__`` — see ``docs/SWE_API_REFERENCE.md`` and PROGRESS.md
for the rationale). Layout::

    <root>/bars/         ticker=<SYM>/date=<D>/bars_<interval>.parquet
    <root>/option_tape/  ticker=<SYM>/date=<D>/trades.parquet
    <root>/option_chain/ ticker=<SYM>/date=<D>/chain.parquet
    <root>/option_quotes/ticker=<SYM>/date=<D>/quotes_<interval>.parquet
    <root>/features/     ticker=<SYM>/date=<D>/<group>.parquet
    <root>/signals/      date=<D>/signals.parquet
    <root>/paper_ledger/ date=<D>/fills.parquet

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

    def has_bars(self, symbol: str, day: date, interval: str = "1m") -> bool:
        """Whether a bars partition exists (any source) — e.g. the parity-ingest
        no-clobber check, without callers touching the partition layout."""
        return (self._partition("bars", symbol, day) / f"bars_{interval}.parquet").exists()

    def read_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        part = self._partition("bars", symbol, day)
        path = part / f"bars_{interval}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no bars at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        meta_path = part / f"bars_{interval}_meta.json"
        # Recover provenance + latency from the sidecar. Like read_tape/read_chain,
        # we REFUSE to guess when it is missing: silently relabelling a parquet of
        # real IBKR/Yahoo bars as SYNTHETIC would violate the honesty guardrail
        # (never mislabel real vs synthetic data).
        if not meta_path.exists():
            raise FileNotFoundError(
                f"missing provenance sidecar {meta_path}; refusing to assume a data source"
            )
        meta = json.loads(meta_path.read_text())
        source = DataSource(meta["source"])
        latency = pd.Timedelta(milliseconds=meta["latency_ms"])
        return BarSeries(symbol.upper(), interval, frame, source, latency)

    # -- option tape ---------------------------------------------------- #
    def write_tape(self, tape: OptionTape, day: date) -> Path:
        part = self._partition("option_tape", tape.symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / "trades.parquet"
        tape.frame.to_parquet(path, engine="pyarrow", index=False)
        (part / "trades_meta.json").write_text(
            json.dumps({"symbol": tape.symbol, "source": tape.source.value})
        )
        return path

    def read_tape(self, symbol: str, day: date) -> OptionTape:
        part = self._partition("option_tape", symbol, day)
        path = part / "trades.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no tape at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        return OptionTape(symbol.upper(), frame, self._read_source(part / "trades_meta.json"))

    # -- option chain --------------------------------------------------- #
    def write_chain(
        self, chain: OptionChainSeries, day: date, *, synthesis: dict | None = None
    ) -> Path:
        """Persist a chain partition; ``synthesis`` (for derived/synthesized
        chains) is stored as a ``chain_synthesis.json`` sidecar beside the
        provenance sidecar — sidecar writing stays the store's job."""
        part = self._partition("option_chain", chain.symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / "chain.parquet"
        chain.frame.to_parquet(path, engine="pyarrow", index=False)
        (part / "chain_meta.json").write_text(
            json.dumps({"symbol": chain.symbol, "source": chain.source.value})
        )
        if synthesis is not None:
            (part / "chain_synthesis.json").write_text(json.dumps(synthesis, indent=1))
        return path

    def has_chain(self, symbol: str, day: date) -> bool:
        """Whether a chain partition exists — e.g. the fused-store preflight for
        options strategies, without callers touching the partition layout."""
        return (self._partition("option_chain", symbol, day) / "chain.parquet").exists()

    def read_chain(self, symbol: str, day: date) -> OptionChainSeries:
        part = self._partition("option_chain", symbol, day)
        path = part / "chain.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no chain at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        return OptionChainSeries(symbol.upper(), frame, self._read_source(part / "chain_meta.json"))

    # -- option quotes (ingest/reconstruction intermediate, not an engine input) #
    def write_option_quotes(
        self, symbol: str, day: date, frame: pd.DataFrame,
        *, source: DataSource, interval: str = "1m",
    ) -> Path:
        """Persist per-contract quote bars (bid/ask/mid at ``interval`` cadence).

        Quotes are the raw material for chain synthesis and parity spot — they are
        not consumed by the engine directly, so there is no PIT container class;
        provenance still round-trips through a sidecar like every other slot.
        """
        part = self._partition("option_quotes", symbol, day)
        part.mkdir(parents=True, exist_ok=True)
        path = part / f"quotes_{interval}.parquet"
        frame.to_parquet(path, engine="pyarrow", index=False)
        (part / f"quotes_{interval}_meta.json").write_text(
            json.dumps({"symbol": symbol.upper(), "source": source.value, "interval": interval})
        )
        return path

    def read_option_quotes(
        self, symbol: str, day: date, interval: str = "1m"
    ) -> tuple[pd.DataFrame, DataSource]:
        part = self._partition("option_quotes", symbol, day)
        path = part / f"quotes_{interval}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no option quotes at {path}")
        frame = pd.read_parquet(path, engine="pyarrow")
        return frame, self._read_source(part / f"quotes_{interval}_meta.json")

    @staticmethod
    def _read_source(meta_path: Path) -> DataSource:
        """Recover persisted provenance; never silently relabel real data.

        If the sidecar is missing we cannot prove provenance, so we refuse to
        guess (a missing sidecar means the writer predates provenance tracking).
        """
        if meta_path.exists():
            return DataSource(json.loads(meta_path.read_text())["source"])
        raise FileNotFoundError(
            f"missing provenance sidecar {meta_path}; refusing to assume a data source"
        )

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
