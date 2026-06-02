"""Paper ledger (T1.3) — PAPER ONLY, no broker, no live orders.

Records gated signals "as if filled" and the resulting fills/trades/equity in the
**same record shape as the backtest** (via :mod:`intraday.execution.records`), so a
future live session and a backtest can be compared directly for divergence
(DESIGN §8). This session there is no live feed, so the ledger is exercised by
mirroring a backtest result (:meth:`from_backtest`) — proving record parity — and
by persisting to the partitioned store.

HARD GUARDRAIL: this module never touches a broker or routes an order. It is a
bookkeeping/observability layer only (README §1). A live streaming loop would feed
the same ``log_*`` methods tick-by-tick; that loop is out of scope until real data
and an explicit go-live decision exist.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import pandas as pd

from ..contracts import Fill, Trade
from .records import fills_to_df, signals_to_df, trades_to_df


class PaperLedger:
    """Append-only paper-trading record with backtest-identical schemas."""

    def __init__(self, initial_capital: float) -> None:
        self.initial_capital = float(initial_capital)
        self.equity = float(initial_capital)
        self.signals: list[dict] = []
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self.equity_curve: list[dict] = []

    # -- live-mode logging hooks (also used by from_backtest) ------------ #
    def log_signal(self, signal: dict) -> None:
        self.signals.append(signal)

    def log_open(self, fill: Fill) -> None:
        self.fills.append(fill)

    def log_close(self, trade: Trade, close_fill: Fill) -> None:
        self.trades.append(trade)
        self.fills.append(close_fill)
        self.equity += trade.net_pnl

    def mark_day_end(self, day: date) -> None:
        self.equity_curve.append(
            {"date": pd.Timestamp(day).isoformat(), "portfolio_value": self.equity}
        )

    # -- parity with the backtest --------------------------------------- #
    @classmethod
    def from_backtest(cls, result) -> "PaperLedger":
        """Build a ledger from a finished backtest — same fills/trades/signals/
        equity, proving the record shapes are identical."""
        led = cls(result.initial_capital)
        led.signals = list(result.signals)
        led.fills = list(result.fills)
        led.trades = list(result.trades)
        led.equity_curve = list(result.equity_curve)
        led.equity = result.final_equity
        return led

    # -- serialization (canonical, shared with the backtest) ------------ #
    def to_fills_df(self) -> pd.DataFrame:
        return fills_to_df(self.fills)

    def to_trades_df(self) -> pd.DataFrame:
        return trades_to_df(self.trades)

    def to_signals_df(self) -> pd.DataFrame:
        return signals_to_df(self.signals)

    def to_equity_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.equity_curve, columns=["date", "portfolio_value"])

    # -- persistence (partitioned store) -------------------------------- #
    def persist(self, store) -> None:
        """Write signals + paper-ledger fills to the store, partitioned by day."""
        by_day_signals: dict[date, list[dict]] = defaultdict(list)
        for s in self.signals:
            d = pd.Timestamp(s["as_of"]).date()
            by_day_signals[d].append(s)
        for d, rows in by_day_signals.items():
            store.write_signals(d, signals_to_df(rows))

        by_day_fills: dict[date, list[Fill]] = defaultdict(list)
        for f in self.fills:
            by_day_fills[pd.Timestamp(f.ts).date()].append(f)
        for d, rows in by_day_fills.items():
            store.write_ledger(d, fills_to_df(rows))
