"""Tests for the read-only live poller (PAPER ONLY).

Verifies the single-shot decision path mirrors the backtester (PIT features → gate
→ reviewers), is deterministic, produces no decision before features exist, and
never needs a broker (it has no order surface — read-only by construction).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from intraday.config import EngineConfig
from intraday.contracts import Verdict
from intraday.data.synthetic import SyntheticDataProvider
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
