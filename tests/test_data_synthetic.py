"""Tests for the deterministic SYNTHETIC data layer.

Covers :class:`SyntheticDataProvider` (bars / chain / tape), the
:func:`asset_kind_for` registry, and the :class:`ParquetStore` round-trips.
Everything here is network-free and deterministic (no Theta, no SWE imports).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday.config import DataConfig, EngineConfig, SessionConfig
from intraday.contracts import (
    BAR_COLUMNS,
    CHAIN_COLUMNS,
    TAPE_COLUMNS,
    AssetKind,
    BarSeries,
    DataSource,
    OptionChainSeries,
    OptionTape,
)
from intraday.data.provider import asset_kind_for
from intraday.data.synthetic import SyntheticDataProvider
from intraday.timeutils import bar_close_index, session_bounds_utc

DAY = date(2026, 5, 18)
OTHER_DAY = date(2026, 5, 19)


# --------------------------------------------------------------------------- #
# asset_kind_for
# --------------------------------------------------------------------------- #


def test_asset_kind_for_known_symbols():
    assert asset_kind_for("SPX") is AssetKind.INDEX
    assert asset_kind_for("VIX") is AssetKind.INDEX
    assert asset_kind_for("SPY") is AssetKind.STOCK
    assert asset_kind_for("QQQ") is AssetKind.STOCK


def test_asset_kind_for_is_case_insensitive_and_defaults_to_stock():
    assert asset_kind_for("spy") is AssetKind.STOCK
    # Unknown symbols default to STOCK.
    assert asset_kind_for("TSLA") is AssetKind.STOCK


def test_asset_kind_multiplier_units():
    # One contract = 100 shares; everything else is 1.
    assert AssetKind.OPTION.multiplier == 100
    assert AssetKind.STOCK.multiplier == 1
    assert AssetKind.INDEX.multiplier == 1


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #


def test_bars_schema_and_shape(spy_bars):
    assert isinstance(spy_bars, BarSeries)
    assert spy_bars.symbol == "SPY"
    assert spy_bars.interval == "1m"
    assert spy_bars.source is DataSource.SYNTHETIC
    assert spy_bars.is_synthetic is True
    frame = spy_bars.frame
    # Exact canonical schema, in canonical order.
    assert tuple(frame.columns) == BAR_COLUMNS
    # 1m RTH session => 390 closes.
    assert len(frame) == 390
    assert frame.index.name == "ts"


def test_bars_index_is_tz_aware_utc_and_matches_close_grid(provider):
    bars = provider.get_bars("SPY", DAY, "1m")
    idx = bars.frame.index
    assert isinstance(idx, pd.DatetimeIndex)
    assert idx.tz is not None
    assert str(idx.tz) == "UTC"
    # Index must equal the canonical bar-close grid for the day.
    expected = bar_close_index(DAY, "1m", provider.session)
    pd.testing.assert_index_equal(idx, expected)
    # First close 13:31 UTC (09:31 ET EDT), last 20:00 UTC (16:00 ET).
    assert idx[0] == pd.Timestamp("2026-05-18 13:31:00", tz="UTC")
    assert idx[-1] == pd.Timestamp("2026-05-18 20:00:00", tz="UTC")


def test_bars_latency_from_config(provider, config):
    bars = provider.get_bars("SPY", DAY, "1m")
    assert bars.latency == pd.Timedelta(milliseconds=config.data.bar_latency_ms)
    assert bars.latency == pd.Timedelta(milliseconds=250)


def test_bars_ohlc_sanity(spy_bars):
    f = spy_bars.frame
    # high must dominate open/close; low must be dominated by open/close.
    assert (f["high"] >= f[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (f["low"] <= f[["open", "close"]].min(axis=1) + 1e-9).all()
    # high >= low always.
    assert (f["high"] >= f["low"] - 1e-9).all()
    # All prices strictly positive and finite (exp of a path).
    assert (f[["open", "high", "low", "close"]] > 0).all().all()
    assert pd.DataFrame(f[["open", "high", "low", "close"]]).notna().all().all()


def test_spx_is_index_with_zero_volume(provider):
    spx = provider.get_bars("SPX", DAY, "1m")
    assert provider.asset_kind("SPX") is AssetKind.INDEX
    assert (spx.frame["volume"] == 0.0).all()
    assert len(spx.frame) == 390


def test_spy_stock_has_positive_volume(spy_bars):
    vol = spy_bars.frame["volume"]
    # Stock instruments carry a U-shaped, strictly positive volume profile.
    assert (vol > 0).all()
    # U-shape: open and close tails heavier than the midday trough.
    midday = vol.iloc[len(vol) // 2]
    assert vol.iloc[0] > midday
    assert vol.iloc[-1] > midday


def test_bars_determinism_same_config(config):
    p1 = SyntheticDataProvider(config.data, config.session)
    p2 = SyntheticDataProvider(config.data, config.session)
    b1 = p1.get_bars("SPY", DAY, "1m")
    b2 = p2.get_bars("SPY", DAY, "1m")
    pd.testing.assert_frame_equal(b1.frame, b2.frame)


def test_bars_different_day_differs(provider):
    b1 = provider.get_bars("SPY", DAY, "1m").frame
    b2 = provider.get_bars("SPY", OTHER_DAY, "1m").frame
    # Same shape and grid offset, but the actual price path must differ.
    assert len(b1) == len(b2) == 390
    assert not b1["close"].to_numpy().tolist() == b2["close"].to_numpy().tolist()


def test_bars_different_seed_differs(config):
    other = SyntheticDataProvider(
        DataConfig(rng_seed=config.data.rng_seed + 1), config.session
    )
    base = SyntheticDataProvider(config.data, config.session)
    b_base = base.get_bars("SPY", DAY, "1m").frame
    b_other = other.get_bars("SPY", DAY, "1m").frame
    assert not b_base["close"].equals(b_other["close"])


@pytest.mark.no_lookahead
def test_bars_available_at_is_causal(spy_bars):
    idx = spy_bars.frame.index
    # As-of exactly a bar close + latency: that bar is included, the next is not.
    bar_close = idx[10]
    as_of = bar_close + spy_bars.latency
    avail = spy_bars.available_at(as_of)
    assert avail.index.max() == bar_close
    assert (avail.index <= bar_close).all()
    # Just before close+latency the bar is NOT yet available.
    earlier = spy_bars.available_at(bar_close + spy_bars.latency - pd.Timedelta(milliseconds=1))
    assert earlier.index.max() == idx[9]


# --------------------------------------------------------------------------- #
# Option chain
# --------------------------------------------------------------------------- #


def test_chain_schema(spy_chain):
    assert isinstance(spy_chain, OptionChainSeries)
    assert spy_chain.symbol == "SPY"
    assert spy_chain.source is DataSource.SYNTHETIC
    frame = spy_chain.frame
    assert tuple(frame.columns) == CHAIN_COLUMNS
    assert not frame.empty


def test_chain_option_type_and_iv_and_oi(spy_chain):
    f = spy_chain.frame
    assert set(f["option_type"].unique()) <= {"C", "P"}
    # Both call and put rows present.
    assert {"C", "P"} <= set(f["option_type"].unique())
    # IV is annualized decimal, strictly positive, and within the clip bounds.
    assert (f["implied_vol"] > 0).all()
    assert (f["implied_vol"] >= 0.05 - 1e-9).all()
    assert (f["implied_vol"] <= 1.5 + 1e-9).all()
    # OI is a non-negative integer-valued count.
    assert (f["open_interest"] >= 0).all()


def test_chain_available_ts_strictly_after_snapshot(spy_chain, config):
    f = spy_chain.frame
    assert (f["available_ts"] > f["snapshot_ts"]).all()
    # The gap is exactly the configured chain latency.
    gap = (f["available_ts"] - f["snapshot_ts"]).unique()
    assert len(gap) == 1
    assert gap[0] == pd.Timedelta(milliseconds=config.data.chain_latency_ms)


def test_chain_snapshot_and_available_tz_utc(spy_chain):
    f = spy_chain.frame
    assert str(f["snapshot_ts"].dt.tz) == "UTC"
    assert str(f["available_ts"].dt.tz) == "UTC"


def test_chain_latest_available_returns_single_snapshot(spy_chain):
    f = spy_chain.frame
    snaps = f["snapshot_ts"].sort_values().unique()
    # Pick an as_of after the SECOND snapshot becomes available => latest is #2.
    second_avail = f.loc[f["snapshot_ts"] == snaps[1], "available_ts"].iloc[0]
    as_of = second_avail + pd.Timedelta(seconds=1)
    latest = spy_chain.latest_available(as_of)
    assert not latest.empty
    # Exactly one snapshot_ts in the result.
    assert latest["snapshot_ts"].nunique() == 1
    assert latest["snapshot_ts"].iloc[0] == snaps[1]


def test_chain_latest_available_empty_before_first(spy_chain):
    f = spy_chain.frame
    first_avail = f["available_ts"].min()
    # One millisecond before the first snapshot is available => nothing usable.
    latest = spy_chain.latest_available(first_avail - pd.Timedelta(milliseconds=1))
    assert latest.empty
    # Empty frame retains the schema.
    assert tuple(latest.columns) == CHAIN_COLUMNS


def test_chain_spot_matches_underlying_at_snapshot(provider):
    # Chain spot is PIT-safe: the last canonical price at/before the snapshot.
    chain = provider.get_option_chain("SPY", DAY)
    f = chain.frame
    snaps = f["snapshot_ts"].unique()
    # Each snapshot has a single spot value.
    for snap in snaps:
        spots = f.loc[f["snapshot_ts"] == snap, "spot"].unique()
        assert len(spots) == 1
        assert spots[0] > 0


def test_chain_determinism(config):
    c1 = SyntheticDataProvider(config.data, config.session).get_option_chain("SPY", DAY)
    c2 = SyntheticDataProvider(config.data, config.session).get_option_chain("SPY", DAY)
    pd.testing.assert_frame_equal(c1.frame, c2.frame)


# --------------------------------------------------------------------------- #
# Option tape
# --------------------------------------------------------------------------- #


def test_tape_schema(spy_tape):
    assert isinstance(spy_tape, OptionTape)
    assert spy_tape.symbol == "SPY"
    assert spy_tape.source is DataSource.SYNTHETIC
    f = spy_tape.frame
    assert tuple(f.columns) == TAPE_COLUMNS
    assert not f.empty


def test_tape_side_inferred_domain(spy_tape):
    sides = set(spy_tape.frame["side_inferred"].unique())
    assert sides <= {"buy", "sell", "mid"}


def test_tape_available_ts_after_ts(spy_tape, config):
    f = spy_tape.frame
    assert (f["available_ts"] > f["ts"]).all()
    gap = (f["available_ts"] - f["ts"]).unique()
    assert len(gap) == 1
    assert gap[0] == pd.Timedelta(milliseconds=config.data.tape_latency_ms)


def test_tape_price_consistency(spy_tape):
    f = spy_tape.frame
    # NBBO sane: bid < ask, price within the quoted band.
    assert (f["nbbo_bid"] < f["nbbo_ask"]).all()
    assert (f["price"] >= f["nbbo_bid"] - 1e-9).all()
    assert (f["price"] <= f["nbbo_ask"] + 1e-9).all()
    assert (f["size"] >= 1).all()
    assert set(f["right"].unique()) <= {"C", "P"}


def test_tape_ts_within_session(provider):
    tape = provider.get_option_tape("SPY", DAY)
    open_utc, close_utc = session_bounds_utc(DAY, provider.session)
    ts = tape.frame["ts"]
    assert str(ts.dt.tz) == "UTC"
    assert (ts > open_utc).all()
    assert (ts <= close_utc).all()
    # Prints are stored in time order.
    assert ts.is_monotonic_increasing


@pytest.mark.no_lookahead
def test_tape_window_available_at_is_causal(spy_tape):
    f = spy_tape.frame
    mid_ts = f["ts"].iloc[len(f) // 2]
    latency = f["available_ts"].iloc[0] - f["ts"].iloc[0]
    as_of = mid_ts + latency
    # Use a wide lookback; only prints with available_ts<=as_of AND ts in window.
    lookback = pd.Timedelta(hours=24)
    win = spy_tape.window_available_at(as_of, lookback)
    assert (win["available_ts"] <= as_of).all()
    assert (win["ts"] > as_of - lookback).all()


def test_tape_determinism(config):
    t1 = SyntheticDataProvider(config.data, config.session).get_option_tape("SPY", DAY)
    t2 = SyntheticDataProvider(config.data, config.session).get_option_tape("SPY", DAY)
    pd.testing.assert_frame_equal(t1.frame, t2.frame)


# --------------------------------------------------------------------------- #
# ParquetStore round-trips
# --------------------------------------------------------------------------- #


def test_store_bars_roundtrip(store, spy_bars):
    store.write_bars(spy_bars, DAY)
    back = store.read_bars("SPY", DAY, "1m")
    assert isinstance(back, BarSeries)
    # Frame round-trips losslessly (tz-aware index preserved). Parquet does not
    # persist the DatetimeIndex freq attribute, so ignore it (values still match).
    pd.testing.assert_frame_equal(back.frame, spy_bars.frame, check_freq=False)
    # Provenance round-trips via the meta sidecar.
    assert back.source is spy_bars.source
    assert back.latency == spy_bars.latency
    assert back.symbol == "SPY"
    assert back.interval == "1m"


def test_store_read_bars_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.read_bars("SPY", DAY, "1m")


def test_store_tape_roundtrip(store, spy_tape):
    store.write_tape(spy_tape, DAY)
    back = store.read_tape("SPY", DAY)
    assert isinstance(back, OptionTape)
    pd.testing.assert_frame_equal(back.frame, spy_tape.frame)
    assert back.source is DataSource.SYNTHETIC
    assert back.symbol == "SPY"


def test_store_chain_roundtrip(store, spy_chain):
    store.write_chain(spy_chain, DAY)
    back = store.read_chain("SPY", DAY)
    assert isinstance(back, OptionChainSeries)
    pd.testing.assert_frame_equal(back.frame, spy_chain.frame)
    assert back.source is DataSource.SYNTHETIC
    assert back.symbol == "SPY"


def test_store_features_roundtrip(store):
    df = pd.DataFrame(
        {
            "available_ts": pd.to_datetime(
                ["2026-05-18 13:31:00", "2026-05-18 13:32:00"], utc=True
            ),
            "vwap": [754.5, 755.1],
            "vwap_sigma": [0.2, 0.25],
        }
    )
    store.write_features("SPY", DAY, "vwap", df)
    back = store.read_features("SPY", DAY, "vwap")
    pd.testing.assert_frame_equal(back, df)


def test_store_read_features_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.read_features("SPY", DAY, "vwap")


def test_store_signals_and_ledger_write(store):
    signals = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-05-18 14:00:00"], utc=True),
            "symbol": ["SPY"],
            "verdict": ["PROCEED"],
            "size": [10],
        }
    )
    sig_path = store.write_signals(DAY, signals)
    assert sig_path.exists()
    pd.testing.assert_frame_equal(
        pd.read_parquet(sig_path, engine="pyarrow"), signals
    )

    ledger = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-05-18 14:00:00"], utc=True),
            "symbol": ["SPY"],
            "kind": ["OPEN"],
            "price": [755.0],
            "size": [10],
        }
    )
    led_path = store.write_ledger(DAY, ledger)
    assert led_path.exists()
    pd.testing.assert_frame_equal(
        pd.read_parquet(led_path, engine="pyarrow"), ledger
    )


def test_store_partition_layout(store, spy_bars):
    path = store.write_bars(spy_bars, DAY)
    # ticker=<SYM>/date=<YYYY-MM-DD> partition layout.
    assert "ticker=SPY" in path.as_posix()
    assert "date=2026-05-18" in path.as_posix()
    assert path.name == "bars_1m.parquet"
    # Meta sidecar exists alongside.
    assert (path.parent / "bars_1m_meta.json").exists()


def test_engine_config_default_wiring():
    cfg = EngineConfig.default()
    assert isinstance(cfg.data, DataConfig)
    assert isinstance(cfg.session, SessionConfig)
    provider = SyntheticDataProvider(cfg.data, cfg.session)
    assert provider.source is DataSource.SYNTHETIC
