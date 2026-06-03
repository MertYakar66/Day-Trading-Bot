"""Ingest captured IBKR underlying payloads into the parquet store.

Separates the bulky FETCH (a network step — the operator's ``ib_insync`` client,
or in dev the IBKR MCP ``get_price_history`` tool) from the deterministic
TRANSFORM (this script). It reads raw IBKR ``get_price_history`` JSON files from
``data_raw/ibkr/`` (one per symbol/interval), remaps each onto the engine's
canonical RTH close grid via :func:`intraday.data.ibkr.ingest_payload`
(``DataSource.IBKR`` provenance), and writes per-session parquet partitions. It
then prints an honest coverage summary. **Reads only — never any order call.**

Run::

    python -m scripts.ingest_ibkr_underlying            # ingest data_raw/ibkr/*.json
    python -m scripts.ingest_ibkr_underlying --raw-dir data_raw/ibkr --store-root data_store

Then backtest on the ingested real data::

    python -m intraday backtest --provider ibkr-store --symbols SPY QQQ \
        --interval 5m --strategy s3 --start <first> --end <last>

IBKR intraday history via this endpoint is shallow (≤1000 bars/request,
back-from-now), so expect only the most recent ~weeks. Deep 2022→today intraday
history comes from the Theta options pull + put-call parity (see
docs/OPERATOR_RUNBOOK.md), NOT from IBKR.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from intraday.config import EngineConfig
from intraday.contracts import AssetKind
from intraday.data.ibkr import IBKR_CONTRACTS, bars_by_day
from intraday.data.store import ParquetStore
from intraday.logging_config import get_logger

logger = get_logger("scripts.ingest_ibkr")

# IBKR chart_step seconds → engine interval string.
_STEP_SECONDS_TO_INTERVAL: dict[int, str] = {
    30: "30s", 60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h",
}


def _infer_symbol_interval(path: Path, payload: dict) -> tuple[str, str]:
    """Symbol from the filename prefix (e.g. ``SPY_5m_1y.json`` → ``SPY``);
    interval from the payload's ``chart_step`` (authoritative)."""
    symbol = path.stem.split("_", 1)[0].upper()
    step = int(payload.get("chart_step", 0))
    interval = _STEP_SECONDS_TO_INTERVAL.get(step)
    if interval is None:
        raise SystemExit(f"{path.name}: unsupported chart_step {step}s")
    return symbol, interval


def ingest_dir(raw_dir: Path, store: ParquetStore, *, min_coverage: float) -> None:
    cfg = EngineConfig.default()
    latency = pd.Timedelta(milliseconds=cfg.data.bar_latency_ms)
    files = sorted(p for p in raw_dir.glob("*.json") if p.name != "manifest.json")
    if not files:
        raise SystemExit(f"no payload JSON files in {raw_dir}")

    total_days = 0
    for path in files:
        payload = json.loads(path.read_text())
        if not payload.get("time"):
            logger.warning("%s: empty payload (error or no data) - skipping", path.name)
            continue
        symbol, interval = _infer_symbol_interval(path, payload)
        contract = IBKR_CONTRACTS.get(symbol)
        kind = (
            AssetKind.INDEX if (contract and contract.security_type == "IND") else AssetKind.STOCK
        )
        by_day = bars_by_day(
            symbol, payload, interval,
            session=cfg.session, latency=latency, asset_kind=kind, min_coverage=min_coverage,
        )
        for day, bars in by_day.items():
            store.write_bars(bars, day)
        days = sorted(by_day)
        total_days += len(days)
        span = f"{days[0]}..{days[-1]}" if days else "(none kept)"
        print(
            f"{path.name:24s} {symbol:4s} {interval:4s} | "
            f"{len(payload['time']):5d} raw bars -> {len(days):3d} sessions kept [{span}]"
        )
    print(f"\nIngested {total_days} session-partitions into {store.root} (source=IBKR).")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest IBKR underlying payloads into the store.")
    p.add_argument("--raw-dir", default="data_raw/ibkr", type=Path)
    p.add_argument("--store-root", default="data_store", type=Path)
    p.add_argument(
        "--min-coverage", type=float, default=0.8,
        help="skip sessions below this fraction of the canonical grid. This is also "
             "the de-facto half-day guard (NYSE 13:00-ET early closes are ~54%% of a "
             "full RTH grid) - do NOT lower below ~0.6 or you will admit half-days "
             "padded with a fabricated flat afternoon.",
    )
    args = p.parse_args(argv)
    if args.min_coverage < 0.6:
        logger.warning(
            "--min-coverage %.2f is low: NYSE early-close half-days (~0.54 coverage) "
            "may be admitted and padded with a flat afternoon. Prefer >= 0.6.",
            args.min_coverage,
        )
    ingest_dir(args.raw_dir, ParquetStore(args.store_root), min_coverage=args.min_coverage)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
