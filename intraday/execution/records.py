"""Canonical record serializers shared by the backtest and the paper ledger.

The whole point of T1.3 is that live paper trading and the backtest emit the
**same record shape**, so live-vs-backtest divergence is measurable (DESIGN §8).
Both paths serialize through these functions, guaranteeing identical schemas.
"""

from __future__ import annotations

import pandas as pd

from ..contracts import Fill, Trade

FILL_COLUMNS: tuple[str, ...] = ("ts", "symbol", "side", "size", "price", "kind", "cost", "reason")
TRADE_COLUMNS: tuple[str, ...] = (
    "symbol", "strategy_id", "side", "size", "instrument",
    "entry_ts", "exit_ts", "entry_price", "exit_price",
    "gross_pnl", "costs", "net_pnl", "exit_reason", "hold_seconds",
)


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


def fills_to_df(fills: list[Fill]) -> pd.DataFrame:
    rows = [
        {
            "ts": _iso(f.ts), "symbol": f.symbol, "side": f.side.value, "size": f.size,
            "price": f.price, "kind": f.kind, "cost": f.cost, "reason": f.reason,
        }
        for f in fills
    ]
    return pd.DataFrame(rows, columns=list(FILL_COLUMNS))


def trades_to_df(trades: list[Trade]) -> pd.DataFrame:
    rows = [
        {
            "symbol": t.symbol, "strategy_id": t.strategy_id, "side": t.side.value,
            "size": t.size, "instrument": t.instrument.value,
            "entry_ts": _iso(t.entry_ts), "exit_ts": _iso(t.exit_ts),
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "gross_pnl": t.gross_pnl, "costs": t.costs, "net_pnl": t.net_pnl,
            "exit_reason": t.exit_reason, "hold_seconds": t.hold_seconds,
        }
        for t in trades
    ]
    return pd.DataFrame(rows, columns=list(TRADE_COLUMNS))


def signals_to_df(signals: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(signals)
