"""Command-line entry point for the intraday engine.

The documented Phase-0 command (prints a NET-OF-COSTS metrics report)::

    python -m intraday backtest --start 2026-05-01 --end 2026-05-29

All data is SYNTHETIC and labelled as such; no broker, no Theta. ``--store``
optionally persists the run's signals + paper-ledger fills to ``data_store/``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .backtest.engine import IntradayBacktester
from .config import DEFAULT_SYMBOLS, EngineConfig
from .contracts import DataSource
from .data.store import ParquetStore
from .data.store_provider import StoreBackedProvider
from .data.synthetic import SyntheticDataProvider
from .logging_config import get_logger
from .metrics import build_report
from .signals.s1_gamma_regime import S1GammaRegime
from .signals.s2_zerodte_vrp import S2ZeroDteVrp
from .signals.s3_vwap_orb import S3VwapOrb
from .signals.s4_orb_breakout import S4OpeningRangeBreakout
from .signals.s5_vwap_momentum import S5VwapMomentum

logger = get_logger("intraday.cli")

# CLI provider name → the DataSource its store partitions were written with.
_STORE_SOURCES: dict[str, DataSource] = {
    "ibkr-store": DataSource.IBKR,
    "yahoo-store": DataSource.YAHOO,
    "theta-store": DataSource.THETA,
}


def _build_provider(args, cfg: EngineConfig, symbols: list[str]):
    """Construct the data provider for the run.

    ``synthetic`` (default) is the deterministic fixture; ``ibkr-store`` /
    ``theta-store`` replay REAL data previously ingested into the parquet store
    (network-free, reproducible). Real-data runs require the data to be ingested
    first — see docs/REAL_DATA.md / docs/OPERATOR_RUNBOOK.md.
    """
    if args.provider == "synthetic":
        return SyntheticDataProvider(cfg.data, cfg.session)
    source = _STORE_SOURCES[args.provider]
    return StoreBackedProvider(
        ParquetStore(args.store_root), source, symbols=symbols, interval=args.interval
    )


def _build_strategies(names: list[str], edge: float, entry_z: float, stop_k: float):
    built = []
    for n in names:
        if n == "s1":
            built.append(S1GammaRegime(edge=edge))
        elif n == "s2":
            built.append(S2ZeroDteVrp())
        elif n == "s3":
            built.append(S3VwapOrb(entry_z=entry_z, stop_k=stop_k, edge=edge))
        elif n == "s4":
            built.append(S4OpeningRangeBreakout(edge=edge))
        elif n == "s5":
            built.append(S5VwapMomentum(entry_z=entry_z, stop_k=stop_k, edge=edge))
        else:
            raise SystemExit(f"unknown strategy {n!r} (choose from s1..s5)")
    return built


def _build_config(nav: float | None) -> EngineConfig:
    cfg = EngineConfig.default()
    if nav is not None:
        cfg = replace(cfg, risk=replace(cfg.risk, paper_nav=nav))
    return cfg


def _render_eval(result, *, n_trials: int) -> str:
    """A concise honesty scorecard appended to the report (clustered-t over trading
    days, bootstrap-CI Sharpe, and the multiple-testing-aware Deflated Sharpe)."""
    from .eval import evaluate_result

    ev = evaluate_result(result, n_trials=n_trials)
    return "\n".join([
        " honesty scorecard (net, clustered by trading day)",
        "-" * 64,
        f" trading-day t-stat : {ev.t_stat:.2f}  (p={ev.p_value:.3f}, n={ev.n_days} days)",
        f" Sharpe (ann, net)  : {ev.sharpe_ann:.2f}  95% CI [{ev.sharpe_ann_ci_lo:.2f}, {ev.sharpe_ann_ci_hi:.2f}]",
        f" P(Sharpe>0)        : {ev.psr_vs_zero:.3f}",
        f" deflated Sharpe    : {ev.deflated_sharpe:.3f}  (n_trials={ev.n_trials}; edge iff >= 0.95)",
        f" VERDICT            : {'EDGE (rare!)' if ev.significant else 'NO demonstrated edge'}",
        "=" * 64,
    ])


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _build_config(args.nav)
    symbols = args.symbols or list(DEFAULT_SYMBOLS)
    provider = _build_provider(args, cfg, symbols)
    strategies = _build_strategies(args.strategy, args.edge, args.entry_z, args.stop_k)
    bt = IntradayBacktester(cfg, provider, strategies)

    logger.info(
        "backtest | source=%s symbols=%s %s..%s interval=%s nav=$%.0f",
        provider.source.value, symbols, args.start, args.end, args.interval, cfg.risk.paper_nav,
    )
    result = bt.run(symbols, args.start, args.end, args.interval)
    report = build_report(result)
    print(report.render())

    if not args.no_eval:
        print(_render_eval(result, n_trials=len(strategies)))

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


def _write_html(path_str: str, html: str, *, label: str, open_browser: bool) -> Path:
    """Write an HTML report (creating parent dirs), print a line, optionally open it."""
    out = Path(path_str)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{label} written: {out.resolve()}  ({len(html):,} bytes)")
    if open_browser:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return out


def cmd_report(args: argparse.Namespace) -> int:
    """Run a backtest and write a self-contained HTML dashboard (paper-only)."""
    from .report import build_dashboard, build_summary

    cfg = _build_config(args.nav)
    symbols = args.symbols or list(DEFAULT_SYMBOLS)
    provider = _build_provider(args, cfg, symbols)
    strategies = _build_strategies(args.strategy, args.edge, args.entry_z, args.stop_k)
    bt = IntradayBacktester(cfg, provider, strategies)

    logger.info(
        "report | source=%s symbols=%s %s..%s interval=%s",
        provider.source.value, symbols, args.start, args.end, args.interval,
    )
    result = bt.run(symbols, args.start, args.end, args.interval)

    universe = None
    if args.universe_json:
        path = Path(args.universe_json)
        if not path.exists():
            raise SystemExit(f"--universe-json not found: {path}")
        universe = json.loads(path.read_text(encoding="utf-8"))

    n_trials = args.n_trials if args.n_trials is not None else len(strategies)
    run_meta = {
        "start": args.start.isoformat(), "end": args.end.isoformat(),
        "strategies": " ".join(args.strategy),
    }
    html = build_dashboard(
        result, n_trials=n_trials, title=args.title,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        universe=universe, run_meta=run_meta,
    )
    _write_html(args.out, html, label="dashboard", open_browser=args.open)

    if args.emit_json:
        summary = build_summary(result, n_trials=n_trials, run_meta=run_meta)
        Path(args.emit_json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"summary JSON written: {Path(args.emit_json).resolve()}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Run each strategy standalone and write a side-by-side comparison page."""
    from .report import build_comparison, build_summary

    cfg = _build_config(args.nav)
    symbols = args.symbols or list(DEFAULT_SYMBOLS)
    provider = _build_provider(args, cfg, symbols)  # read-only; reused across runs
    run_meta = {
        "start": args.start.isoformat(), "end": args.end.isoformat(),
        "strategies": " ".join(args.strategy),
    }
    runs: list[tuple[str, object]] = []
    for name in args.strategy:
        strat = _build_strategies([name], args.edge, args.entry_z, args.stop_k)
        bt = IntradayBacktester(cfg, provider, strat)
        logger.info("compare | %s source=%s %s..%s interval=%s",
                    name, provider.source.value, args.start, args.end, args.interval)
        runs.append((name, bt.run(symbols, args.start, args.end, args.interval)))

    html = build_comparison(
        runs, title=args.title,
        generated_at=datetime.now().isoformat(timespec="seconds"), run_meta=run_meta,
    )
    _write_html(args.out, html, label="comparison", open_browser=args.open)

    if args.emit_json:
        summaries = [
            build_summary(res, n_trials=len(runs), run_meta={**run_meta, "strategies": name})
            for name, res in runs
        ]
        Path(args.emit_json).write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        print(f"summary JSON written: {Path(args.emit_json).resolve()}")
    return 0


def cmd_report_index(args: argparse.Namespace) -> int:
    """Build a static index linking every dashboard/comparison HTML in a directory."""
    from .report import build_index

    directory = Path(args.dir)
    if not directory.is_dir():
        raise SystemExit(f"--dir is not a directory: {directory}")
    out = Path(args.out) if args.out else directory / "index.html"
    html = build_index(
        directory, title=args.title,
        generated_at=datetime.now().isoformat(timespec="seconds"), index_name=out.name,
    )
    _write_html(str(out), html, label="index", open_browser=args.open)
    return 0


def _add_run_args(sp: argparse.ArgumentParser) -> None:
    """Arguments shared by every subcommand that runs the engine (backtest/report)."""
    sp.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 1))
    sp.add_argument("--end", type=date.fromisoformat, default=date(2026, 5, 29))
    sp.add_argument("--symbols", nargs="*", default=None, help="default: SPX SPY QQQ")
    sp.add_argument("--interval", default="1m")
    sp.add_argument(
        "--provider", choices=["synthetic", "ibkr-store", "yahoo-store", "theta-store"],
        default="synthetic",
        help="data source: 'synthetic' (default, deterministic fixture) or replay "
             "REAL data ingested into the store ('ibkr-store'/'yahoo-store' "
             "underlying, 'theta-store' options). Real-data runs need prior ingestion "
             "(docs/REAL_DATA.md).",
    )
    sp.add_argument("--store-root", dest="store_root", default="data_store",
                    help="parquet store root for --provider *-store (default: data_store)")
    sp.add_argument(
        "--strategy", nargs="+", default=["s3"], choices=["s1", "s2", "s3", "s4", "s5"],
        help="strategies (default s3). s1/s2 load option features (slower); "
             "s3=VWAP reversion, s4=ORB breakout, s5=VWAP momentum (underlying-only).",
    )
    sp.add_argument("--nav", type=float, default=None, help="paper NAV (assumption)")
    sp.add_argument("--entry-z", dest="entry_z", type=float, default=2.0)
    sp.add_argument("--stop-k", dest="stop_k", type=float, default=1.0)
    sp.add_argument(
        "--edge", dest="edge", type=float, default=0.10,
        help="S3 mean-reversion edge over the gambler's-ruin fair baseline "
             "(0.0 = no edge → gate refuses all trades)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="intraday", description="Intraday engine (paper-only).")
    sub = p.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="Run the Phase-0 backtest and print metrics.")
    _add_run_args(bt)
    bt.add_argument("--no-eval", dest="no_eval", action="store_true",
                    help="skip the honesty scorecard (clustered-t / bootstrap-CI Sharpe / deflated Sharpe)")
    bt.add_argument("--store", action="store_true", help="persist signals + ledger")
    bt.set_defaults(func=cmd_backtest)

    rp = sub.add_parser(
        "report",
        help="Run a backtest and write a self-contained HTML dashboard (offline, paper-only).",
    )
    _add_run_args(rp)
    rp.add_argument("--out", default="dashboard.html",
                    help="output HTML path (default: dashboard.html)")
    rp.add_argument("--title", default="Intraday engine — backtest dashboard")
    rp.add_argument("--n-trials", dest="n_trials", type=int, default=None,
                    help="deflated-Sharpe trial count (default: number of strategies run)")
    rp.add_argument("--universe-json", dest="universe_json", default=None,
                    help="optional cross-sectional eval JSON (scripts/eval_real_universe.py) to embed")
    rp.add_argument("--emit-json", dest="emit_json", default=None,
                    help="also write a machine-readable summary JSON to this path")
    rp.add_argument("--open", action="store_true", help="open the dashboard in a browser after writing")
    rp.set_defaults(func=cmd_report)

    cp = sub.add_parser(
        "compare",
        help="Run several strategies standalone and write a side-by-side comparison page.",
    )
    _add_run_args(cp)
    cp.set_defaults(strategy=["s3", "s4", "s5"])  # comparison is most useful multi-strategy
    cp.add_argument("--out", default="compare.html", help="output HTML path (default: compare.html)")
    cp.add_argument("--title", default="Intraday engine — strategy comparison")
    cp.add_argument("--emit-json", dest="emit_json", default=None,
                    help="also write a per-strategy summary JSON array to this path")
    cp.add_argument("--open", action="store_true", help="open the comparison in a browser after writing")
    cp.set_defaults(func=cmd_compare)

    ix = sub.add_parser(
        "report-index",
        help="Build a static index.html linking every report in a directory.",
    )
    ix.add_argument("--dir", required=True, help="directory of *.html reports to index")
    ix.add_argument("--out", default=None, help="output path (default: <dir>/index.html)")
    ix.add_argument("--title", default="Intraday engine — report index")
    ix.add_argument("--open", action="store_true", help="open the index in a browser after writing")
    ix.set_defaults(func=cmd_report_index)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
