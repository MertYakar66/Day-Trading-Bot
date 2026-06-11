"""Tests for the read-only live poller (PAPER ONLY).

Verifies the single-shot decision path mirrors the backtester (PIT features → gate
→ reviewers), is deterministic, produces no decision before features exist, and
never needs a broker (it has no order surface — read-only by construction).
Also covers decision persistence: a persisted poll uses the backtest-identical
signal schema (record parity, DESIGN §8).
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pandas as pd

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import DataSource, Verdict
from intraday.data.store import ParquetStore
from intraday.data.synthetic import SyntheticDataProvider
from intraday.execution.paper_ledger import PaperLedger
from intraday.live import LiveDecision, LivePoller
from intraday.signals.s3_vwap_orb import S3VwapOrb
from intraday.timeutils import bar_close_index

DAY = date(2026, 5, 4)
_VERDICTS = {v.name for v in Verdict}


def _poller(strategies):
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    return LivePoller(cfg, prov, strategies, interval="5m"), cfg


def test_poll_produces_valid_decisions_somewhere():
    poller, cfg = _poller([S3VwapOrb(entry_z=0.1, edge=0.5)])  # permissive → triggers
    idx = bar_close_index(DAY, "5m", cfg.session)
    lat = pd.Timedelta(milliseconds=cfg.data.bar_latency_ms)
    decisions: list[LiveDecision] = []
    for ts in idx[20:]:
        decisions += poller.poll("SPY", DAY, ts + lat + pd.Timedelta(seconds=1))
    assert decisions, "expected at least one gated decision across the session"
    for d in decisions:
        assert d.verdict in _VERDICTS
        assert d.side in ("long", "short")
        assert isinstance(d.tradeable, bool)
        assert d.size >= 0


def test_poll_is_deterministic():
    poller, cfg = _poller([S3VwapOrb(entry_z=0.1, edge=0.5)])
    ts = bar_close_index(DAY, "5m", cfg.session)[40] + pd.Timedelta(seconds=1)
    a = poller.poll("SPY", DAY, ts)
    b = poller.poll("SPY", DAY, ts)
    assert a == b


def test_no_decision_before_features_exist():
    poller, cfg = _poller([S3VwapOrb()])
    # Just after the open, before the opening range / VWAP bands are defined.
    open_close = bar_close_index(DAY, "5m", cfg.session)[0]
    assert poller.poll("SPY", DAY, open_close + pd.Timedelta(seconds=1)) == []


def test_poller_has_no_order_surface():
    """Paper-only by construction: the poller exposes no order/broker method."""
    poller, _ = _poller([S3VwapOrb()])
    for attr in dir(poller):
        assert "order" not in attr.lower()
        assert "broker" not in attr.lower()


# --------------------------------------------------------------------------- #
# Persistence: backtest-identical signal records (DESIGN §8 record parity)
# --------------------------------------------------------------------------- #
def _first_decisions() -> list[LiveDecision]:
    poller, cfg = _poller([S3VwapOrb(entry_z=0.1, edge=0.5)])
    idx = bar_close_index(DAY, "5m", cfg.session)
    lat = pd.Timedelta(milliseconds=cfg.data.bar_latency_ms)
    for ts in idx[20:]:
        decisions = poller.poll("SPY", DAY, ts + lat + pd.Timedelta(seconds=1))
        if decisions:
            return decisions
    raise AssertionError("permissive S3 produced no decision all session")


def test_signal_row_schema_matches_backtest():
    """A persisted poll row must carry exactly the keys (and order) the backtest
    writes, or live-vs-backtest divergence stops being diffable."""
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S3VwapOrb(entry_z=0.1, edge=0.5)])
    res = bt.run(["SPY"], DAY, DAY, "5m")
    assert res.signals, "backtest must record signals for the parity check"

    poll_row = _first_decisions()[0].to_signal_row()
    assert list(poll_row.keys()) == list(res.signals[0].keys())


def test_persisted_poll_roundtrips_through_store(tmp_path):
    decisions = _first_decisions()
    ledger = PaperLedger(EngineConfig.default().risk.paper_nav)
    for d in decisions:
        ledger.log_signal(d.to_signal_row())
    ledger.persist(ParquetStore(tmp_path / "persist"))

    path = tmp_path / "persist" / "signals" / f"date={DAY}" / "signals.parquet"
    assert path.exists()
    df = pd.read_parquet(path)
    assert len(df) == len(decisions)
    assert list(df.columns) == list(decisions[0].to_signal_row().keys())


def test_script_persist_flag_writes_signals(tmp_path, monkeypatch):
    """End-to-end through scripts.live_paper_poll: --persist-root writes the
    day's poll record; without it the script writes nothing."""
    from scripts import live_paper_poll

    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bars = prov.get_bars("SPY", DAY, "5m")
    store = ParquetStore(tmp_path / "store")
    store.write_bars(dataclasses.replace(bars, source=DataSource.YAHOO), DAY)
    # Permissive S3 so the deterministic synthetic session yields a decision.
    monkeypatch.setitem(
        live_paper_poll._STRAT, "s3", lambda: S3VwapOrb(entry_z=0.1, edge=0.5)
    )
    argv = [
        "--provider", "yahoo-store", "--store-root", str(tmp_path / "store"),
        "--symbols", "SPY", "--strategies", "s3",
        "--start", "2026-05-04", "--end", "2026-05-04",
    ]
    assert live_paper_poll.main(argv) == 0
    assert not (tmp_path / "persist").exists()  # print-only without the flag

    assert live_paper_poll.main([*argv, "--persist-root", str(tmp_path / "persist")]) == 0
    # The synthetic 2026-05-04 session deterministically yields a flatten-time
    # decision under the permissive S3, so the record must exist.
    sig = tmp_path / "persist" / "signals" / f"date={DAY}" / "signals.parquet"
    assert sig.exists()
    df = pd.read_parquet(sig)
    assert len(df) >= 1
    assert {"as_of", "symbol", "strategy_id", "verdict", "ev_gross", "ev_net"} <= set(df.columns)
