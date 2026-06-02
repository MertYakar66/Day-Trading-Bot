"""Command-line entry point for the intraday engine.

The documented Phase-0 command (prints a NET-OF-COSTS metrics report)::

    python -m intraday backtest --start 2026-05-01 --end 2026-05-29

All data is SYNTHETIC and labelled as such; no broker, no Theta. ``--store``
optionally persists the run's signals + paper-ledger fills to ``data_store/``.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date

import pandas as pd

from .backtest.engine import IntradayBacktester
from .config import DEFAULT_SYMBOLS, EngineConfig
from .data.store import ParquetStore
from .data.synthetic import SyntheticDataProvider
from .logging_config import get_logger
from .metrics import build_report
from .signals.s3_vwap_orb import S3VwapOrb

logger = get_logger("intraday.cli")


def _build_config(nav: float | None) -> EngineConfig:
    cfg = EngineConfig.default()
    if nav is not None:
        cfg = replace(cfg, risk=replace(cfg.risk, paper_nav=nav))
    return cfg


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _build_config(args.nav)
    provider = SyntheticDataProvider(cfg.data, cfg.session)
    strategies = [S3VwapOrb(entry_z=args.entry_z, stop_k=args.stop_k, edge=args.edge)]
    bt = IntradayBacktester(cfg, provider, strategies)

    symbols = args.symbols or list(DEFAULT_SYMBOLS)
    logger.info(
        "Phase-0 backtest | source=%s symbols=%s %s..%s interval=%s nav=$%.0f",
        provider.source.value, symbols, args.start, args.end, args.interval, cfg.risk.paper_nav,
    )
    result = bt.run(symbols, args.start, args.end, args.interval)
    report = build_report(result)
    print(report.render())

    if args.store:
        store = ParquetStore()
        if result.signals:
            store.write_signals(args.start, pd.DataFrame(result.signals))
        ledger_rows = [
            {
                "ts": pd.Timestamp(f.ts).isoformat(), "symbol": f.symbol,
                "side": f.side.value, "size": f.size, "price": f.price,
                "kind": f.kind, "cost": f.cost, "reason": f.reason,
            }
            for f in result.fills
        ]
        if ledger_rows:
            store.write_ledger(args.start, pd.DataFrame(ledger_rows))
        logger.info("persisted %d signals, %d fills to data_store/", len(result.signals), len(ledger_rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="intraday", description="Intraday engine (paper-only).")
    sub = p.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="Run the Phase-0 backtest and print metrics.")
    bt.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 1))
    bt.add_argument("--end", type=date.fromisoformat, default=date(2026, 5, 29))
    bt.add_argument("--symbols", nargs="*", default=None, help="default: SPX SPY QQQ")
    bt.add_argument("--interval", default="1m")
    bt.add_argument("--nav", type=float, default=None, help="paper NAV (assumption)")
    bt.add_argument("--entry-z", dest="entry_z", type=float, default=2.0)
    bt.add_argument("--stop-k", dest="stop_k", type=float, default=1.0)
    bt.add_argument(
        "--edge", dest="edge", type=float, default=0.10,
        help="S3 mean-reversion edge over the gambler's-ruin fair baseline "
             "(0.0 = no edge → gate refuses all trades)",
    )
    bt.add_argument("--store", action="store_true", help="persist signals + ledger")
    bt.set_defaults(func=cmd_backtest)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
