"""Read-only paper poll demo — gated decisions at the latest stored session.

Demonstrates "what you can do with IBKR/Yahoo READ-ONLY": pull recent real bars
(replayed from the store here; the same LivePoller works against a live read-only
provider), build PIT features, and surface gated PAPER decisions. **No orders are
ever placed — there is no broker call anywhere in this path.**

Run::

    python -m scripts.live_paper_poll --provider yahoo-store --store-root data_raw/store_yahoo \
        --symbols SPY QQQ AAPL --strategies s3 s4 s5
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intraday.config import EngineConfig  # noqa: E402
from intraday.contracts import DataSource  # noqa: E402
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.data.store_provider import StoreBackedProvider  # noqa: E402
from intraday.live import LivePoller  # noqa: E402
from intraday.signals.s3_vwap_orb import S3VwapOrb  # noqa: E402
from intraday.signals.s4_orb_breakout import S4OpeningRangeBreakout  # noqa: E402
from intraday.signals.s5_vwap_momentum import S5VwapMomentum  # noqa: E402
from intraday.timeutils import flatten_time_utc  # noqa: E402

_SRC = {"yahoo-store": DataSource.YAHOO, "ibkr-store": DataSource.IBKR}
_STRAT = {"s3": lambda: S3VwapOrb(), "s4": lambda: S4OpeningRangeBreakout(), "s5": lambda: S5VwapMomentum()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only paper poll (NO ORDERS).")
    p.add_argument("--provider", choices=list(_SRC), default="yahoo-store")
    p.add_argument("--store-root", default="data_raw/store_yahoo")
    p.add_argument("--symbols", nargs="*", default=["SPY", "QQQ"])
    p.add_argument("--interval", default="5m")
    p.add_argument("--strategies", nargs="+", default=["s3", "s4", "s5"])
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 12, 31))
    args = p.parse_args(argv)

    cfg = EngineConfig.default()
    store = ParquetStore(args.store_root)
    provider = StoreBackedProvider(store, _SRC[args.provider], symbols=args.symbols, interval=args.interval)
    strategies = [_STRAT[s]() for s in args.strategies]
    poller = LivePoller(cfg, provider, strategies, interval=args.interval)

    days = provider.trading_days(args.start, args.end)
    if not days:
        raise SystemExit(f"no stored sessions for {args.symbols} in {args.store_root}")
    day = days[-1]                          # most recent stored session
    as_of = flatten_time_utc(day, cfg.session)  # decision near the session's end

    print("=" * 72)
    print(f" READ-ONLY PAPER POLL  (NO ORDERS)  session={day}  as_of={as_of}  src={args.provider}")
    print("=" * 72)
    print(" NOTE: PROCEED/*TRADE* reflects the gate under each strategy's ASSUMED edge")
    print(" (default 0.10). The powered real-data eval found NO demonstrated edge for")
    print(" S3/S4/S5 — these are advisory paper signals, not evidence of alpha.")
    print("-" * 72)
    print(f" {'symbol':6} {'strat':18} {'verdict':8} {'side':5} {'size':>5} {'ev_net$':>9} {'reason'}")
    print("-" * 72)
    any_dec = False
    for sym in args.symbols:
        try:
            decisions = poller.poll(sym, day, as_of)
        except Exception as e:
            print(f" {sym:6} (no data: {type(e).__name__})")
            continue
        for d in decisions:
            any_dec = True
            flag = "*TRADE*" if d.tradeable else ""
            print(f" {sym:6} {d.strategy_id:18} {d.verdict:8} {d.side:5} {d.size:>5} "
                  f"{d.ev_net:>9.2f} {d.reason} {flag}")
    if not any_dec:
        print(" (no strategy produced a signal at this poll time)")
    print("=" * 72)
    print(" Paper-only: decisions are advisory; nothing was sent to any broker.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
