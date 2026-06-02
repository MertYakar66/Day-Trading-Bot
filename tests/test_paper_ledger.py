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
