"""Paper ledger (T1.3): record-shape parity with the backtest + persistence.

PAPER ONLY — the ledger is a bookkeeping layer; these tests assert it mirrors a
backtest result exactly (same trades/fills/net) and persists in the canonical
schema, so live-vs-backtest divergence will be measurable later (DESIGN §8).
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.data.synthetic import SyntheticDataProvider
from intraday.execution.paper_ledger import PaperLedger
from intraday.execution.records import FILL_COLUMNS, TRADE_COLUMNS
from intraday.signals.s3_vwap_orb import S3VwapOrb

pytestmark = pytest.mark.swe


def _backtest():
    cfg = EngineConfig.default()
    bt = IntradayBacktester(cfg, SyntheticDataProvider(cfg.data, cfg.session), [S3VwapOrb()])
    return bt.run(["SPY"], date(2026, 5, 4), date(2026, 5, 6), "1m")


def test_from_backtest_parity():
    result = _backtest()
    led = PaperLedger.from_backtest(result)
    assert len(led.trades) == len(result.trades)
    assert len(led.fills) == len(result.fills)
    assert len(led.signals) == len(result.signals)
    assert led.equity == pytest.approx(result.final_equity)
    assert led.equity == pytest.approx(
        result.initial_capital + sum(t.net_pnl for t in result.trades)
    )


def test_canonical_schemas():
    led = PaperLedger.from_backtest(_backtest())
    assert list(led.to_fills_df().columns) == list(FILL_COLUMNS)
    assert list(led.to_trades_df().columns) == list(TRADE_COLUMNS)
    # net == gross − costs holds in the serialized trade rows too.
    tdf = led.to_trades_df()
    if not tdf.empty:
        assert (tdf["net_pnl"] - (tdf["gross_pnl"] - tdf["costs"])).abs().max() < 1e-6


def test_live_hooks_accumulate_equity_and_match_from_backtest():
    """Drive the live-mode hooks tick-by-tick (the path a real streaming loop would
    use) and assert the running equity + equity_curve are correct AND identical to a
    from_backtest ledger built over the same records — i.e. live and backtest book
    the same way (DESIGN §8)."""
    from types import SimpleNamespace

    import pandas as pd

    from intraday.contracts import AssetKind, Fill, Side, Trade

    def _fill(ts, kind, price):
        return Fill(ts=pd.Timestamp(ts, tz="UTC"), symbol="SPY", side=Side.LONG,
                    size=10, price=price, kind=kind, cost=0.5)

    def _trade(entry, exit_, net):
        return Trade(symbol="SPY", strategy_id="s3", side=Side.LONG, size=10,
                     instrument=AssetKind.STOCK,
                     entry_ts=pd.Timestamp(entry, tz="UTC"), exit_ts=pd.Timestamp(exit_, tz="UTC"),
                     entry_price=100.0, exit_price=100.0 + net / 10.0,
                     gross_pnl=net + 1.0, costs=1.0, net_pnl=net, exit_reason="target")

    led = PaperLedger(100_000.0)
    led.log_signal({"as_of": "2026-05-04T14:00:00+00:00", "symbol": "SPY"})
    led.log_open(_fill("2026-05-04 14:00", "OPEN", 100.0))           # day 1: +250 net
    led.log_close(_trade("2026-05-04 14:00", "2026-05-04 15:00", 250.0),
                  _fill("2026-05-04 15:00", "CLOSE", 125.0))
    led.mark_day_end(date(2026, 5, 4))
    led.log_open(_fill("2026-05-05 14:00", "OPEN", 100.0))           # day 2: -100 net
    led.log_close(_trade("2026-05-05 14:00", "2026-05-05 15:00", -100.0),
                  _fill("2026-05-05 15:00", "CLOSE", 90.0))
    led.mark_day_end(date(2026, 5, 5))

    assert led.equity == pytest.approx(100_000.0 + 250.0 - 100.0)
    assert [r["portfolio_value"] for r in led.equity_curve] == [
        pytest.approx(100_250.0), pytest.approx(100_150.0)
    ]
    assert len(led.fills) == 4 and len(led.trades) == 2 and len(led.signals) == 1

    # The same records via from_backtest book identically (live == backtest accounting).
    result = SimpleNamespace(
        initial_capital=100_000.0, final_equity=led.equity, signals=led.signals,
        fills=led.fills, trades=led.trades, equity_curve=led.equity_curve,
    )
    mirror = PaperLedger.from_backtest(result)
    assert mirror.equity == pytest.approx(led.equity)
    assert mirror.equity_curve == led.equity_curve


def test_persist_round_trip(store):
    result = _backtest()
    led = PaperLedger.from_backtest(result)
    led.persist(store)
    # The first trading day's ledger fills are persisted and readable.
    day = date(2026, 5, 4)
    import pandas as pd

    path = store._date_partition("paper_ledger", day) / "fills.parquet"
    assert path.exists()
    df = pd.read_parquet(path)
    assert list(df.columns) == list(FILL_COLUMNS)
    assert len(df) > 0
