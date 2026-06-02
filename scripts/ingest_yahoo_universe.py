"""Ingest captured Yahoo payloads (data_raw/yahoo/*.json) into the parquet store.

Deterministic, network-free transform (fetch is the separate
``scripts.fetch_yahoo_universe`` step). Each file ``<SYM>_<interval>_<range>.json``
is remapped onto the canonical RTH close grid via
:func:`intraday.data.yahoo.bars_by_day` (``DataSource.YAHOO`` provenance) and
written as per-session parquet partitions. Prints a coverage summary.

Run::

    python -m scripts.ingest_yahoo_universe                       # ingest data_raw/yahoo/*.json
    python -m scripts.ingest_yahoo_universe --raw-dir data_raw/yahoo --store-root data_store

Then backtest on the real universe::

    python -m intraday backtest --provider yahoo-store --symbols SPY QQQ AAPL ... \
        --interval 5m --strategy s3 --start <first> --end <last>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from intraday.config import EngineConfig
from intraday.data.store import ParquetStore
from intraday.data.yahoo import bars_by_day
from intraday.logging_config import get_logger

logger = get_logger("scripts.ingest_yahoo")


def _infer_symbol_interval(path: Path) -> tuple[str, str]:
    """``SPY_5m_60d.json`` → (``SPY``, ``5m``)."""
    parts = path.stem.split("_")
    if len(parts) < 2:
        raise SystemExit(f"{path.name}: cannot infer symbol/interval from filename")
    return parts[0].upper(), parts[1]


def ingest_dir(raw_dir: Path, store: ParquetStore, *, min_coverage: float) -> None:
    cfg = EngineConfig.default()
    latency = pd.Timedelta(milliseconds=cfg.data.bar_latency_ms)
    files = sorted(p for p in raw_dir.glob("*.json") if p.name != "manifest.json")
    if not files:
        raise SystemExit(f"no payload JSON files in {raw_dir}")

    total_days = 0
    for path in files:
        payload = json.loads(path.read_text())
        symbol, interval = _infer_symbol_interval(path)
        try:
            by_day = bars_by_day(
                symbol, payload, interval,
                session=cfg.session, latency=latency, min_coverage=min_coverage,
            )
        except Exception as e:  # malformed/empty payload — skip, do not abort the batch
            print(f"{path.name:26s} {symbol:6s} {interval:4s} | SKIP ({type(e).__name__}: {str(e)[:60]})")
            continue
        for day, bars in by_day.items():
            store.write_bars(bars, day)
        days = sorted(by_day)
        total_days += len(days)
        span = f"{days[0]}..{days[-1]}" if days else "(none kept)"
        print(f"{path.name:26s} {symbol:6s} {interval:4s} | {len(days):3d} sessions kept [{span}]")
    print(f"\nIngested {total_days} session-partitions into {store.root} (source=YAHOO).")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest Yahoo universe payloads into the store.")
    p.add_argument("--raw-dir", default="data_raw/yahoo", type=Path)
    p.add_argument("--store-root", default="data_store", type=Path)
    p.add_argument("--min-coverage", type=float, default=0.8,
                   help="skip sessions below this fraction of the canonical grid")
    args = p.parse_args(argv)
    ingest_dir(args.raw_dir, ParquetStore(args.store_root), min_coverage=args.min_coverage)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
