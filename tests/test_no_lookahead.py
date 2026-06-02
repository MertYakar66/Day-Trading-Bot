"""No-look-ahead property tests — the single most important invariant (DESIGN §2.3).

Each test fails if a future bar/print can change a value computed "as of" an
earlier time. We prove this two ways:
1. Causality: truncating future rows does not change past feature values.
2. PIT robustness: appending future rows does not change a feature/decision row
   sampled at an earlier ``as_of``.
3. Equivalence: the backtest's fast positional feature read at bar ``i`` equals
   the general PIT ``FeaturePipeline.row(as_of_i)`` — so the engine's speed
   optimization cannot smuggle in look-ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday.config import EngineConfig
from intraday.features.base import latest_value
from intraday.features.ofi import ofi_at
from intraday.features.pipeline import FeaturePipeline
from intraday.features.vwap import vwap_frame

pytestmark = pytest.mark.no_lookahead


def test_vwap_is_causal(spy_bars):
    """vwap_frame value at row i is unchanged by truncating bars after i."""
    full = vwap_frame(spy_bars.frame)
    for i in (30, 120, 300, len(spy_bars.frame) - 1):
        truncated = vwap_frame(spy_bars.frame.iloc[: i + 1])
        assert truncated["vwap"].iloc[i] == pytest.approx(full["vwap"].iloc[i])
        assert truncated["vwap_sigma"].iloc[i] == pytest.approx(full["vwap_sigma"].iloc[i])


def test_pipeline_row_unaffected_by_future_bars(spy_bars, spy_chain, spy_tape, config):
    """A FeatureRow at as_of is identical whether or not future data exists."""
    pipe = FeaturePipeline(config)
    frame = spy_bars.frame
    i = 180
    as_of = frame.index[i] + spy_bars.latency

    # Full-day inputs.
    full = pipe.row("SPY", spy_bars.symbol and frame.index[0].date(), as_of,
                    bars=spy_bars, chain=spy_chain, tape=spy_tape, interval="1m")

    # Inputs truncated to only what has arrived by as_of (simulate "no future").
    from dataclasses import replace as dc_replace

    bars_cut = dc_replace(spy_bars, frame=frame.loc[frame.index <= as_of])
    chain_cut = dc_replace(spy_chain, frame=spy_chain.frame.loc[spy_chain.frame["available_ts"] <= as_of])
    tape_cut = dc_replace(spy_tape, frame=spy_tape.frame.loc[spy_tape.frame["available_ts"] <= as_of])
    cut = pipe.row("SPY", frame.index[0].date(), as_of,
                   bars=bars_cut, chain=chain_cut, tape=tape_cut, interval="1m")

    assert cut.vwap == pytest.approx(full.vwap)
    assert cut.vwap_dev_sigma == pytest.approx(full.vwap_dev_sigma)
    assert cut.ofi == pytest.approx(full.ofi)
    assert cut.atm_iv == pytest.approx(full.atm_iv)
    assert cut.gex_total == pytest.approx(full.gex_total)
    assert cut.last_price == pytest.approx(full.last_price)


def test_ofi_ignores_future_prints(spy_tape):
    """OFI at as_of is unchanged when later prints are appended."""
    frame = spy_tape.frame
    as_of = frame["ts"].iloc[200] + pd.Timedelta(seconds=1)
    from dataclasses import replace as dc_replace

    early = dc_replace(spy_tape, frame=frame.loc[frame["ts"] <= as_of])
    full_val = ofi_at(spy_tape, as_of, pd.Timedelta(minutes=5))
    early_val = ofi_at(early, as_of, pd.Timedelta(minutes=5))
    assert full_val == pytest.approx(early_val)


def test_latest_value_respects_latency(spy_bars):
    """A bar closing at t is not visible until t + latency."""
    s = spy_bars.frame["close"]
    t = s.index[100]
    lat = spy_bars.latency
    # Just before arrival: bar 100 not yet visible → returns bar 99's value.
    just_before = latest_value(s, t + lat - pd.Timedelta(milliseconds=1), lat)
    assert just_before == pytest.approx(s.iloc[99])
    # At arrival: bar 100 visible.
    at_arrival = latest_value(s, t + lat, lat)
    assert at_arrival == pytest.approx(s.iloc[100])


def test_backtest_positional_equals_pit_row(spy_bars, config):
    """The engine's positional feature read at bar i == FeaturePipeline.row(as_of_i).

    This guards the backtest's speed optimization against accidental look-ahead.
    """
    from intraday.backtest.engine import IntradayBacktester
    from intraday.data.synthetic import SyntheticDataProvider
    from intraday.signals.s3_vwap_orb import S3VwapOrb

    bt = IntradayBacktester(config, SyntheticDataProvider(config.data, config.session), [S3VwapOrb()])
    pipe = bt.pipeline
    day = spy_bars.frame.index[0].date()
    sf = pipe.precompute(spy_bars, day)
    d = {
        "close": spy_bars.frame["close"].to_numpy(),
        "vwap": sf.vwap["vwap"].to_numpy(),
        "sigma": sf.vwap["vwap_sigma"].to_numpy(),
        "dev": sf.vwap["vwap_dev_sigma"].to_numpy(),
        "or_known_i": bt._orb_known_index(spy_bars.frame.index, sf),
        "sf": sf,
    }
    for i in (40, 150, 320):
        as_of = spy_bars.frame.index[i] + spy_bars.latency
        positional = bt._feature_row("SPY", as_of, d, i)
        pit = pipe.row("SPY", day, as_of, bars=spy_bars, session=sf, interval="1m")
        assert positional.vwap == pytest.approx(pit.vwap)
        # dev may be None early; compare with NaN-safe logic.
        if positional.vwap_dev_sigma is None:
            assert pit.vwap_dev_sigma is None
        else:
            assert positional.vwap_dev_sigma == pytest.approx(pit.vwap_dev_sigma)
        assert positional.last_price == pytest.approx(pit.last_price)
