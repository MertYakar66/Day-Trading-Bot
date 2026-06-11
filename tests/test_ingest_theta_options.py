"""Tests for the Theta ingest + chain synthesizer — proof by construction.

Quote fixtures are built FROM a known spot path and a known sigma* via the
vendor Black-Scholes pricer (the parity.py testing pattern), so the synthesizer
is held to recovery: parity spot ≈ true spot, inverted IV ≈ sigma*. The PIT
rules (prior-session OI, past-only quotes, available_ts stamping, no-look-ahead
property) and the honesty refusals (no parity pair, junk quotes, SPX, non-0DTE
chains) each have a dedicated test.

Marked ``swe``: the synthesizer wraps the vendor option pricer.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from intraday.config import EngineConfig
from intraday.contracts import CHAIN_COLUMNS, TAPE_COLUMNS, DataSource, OptionChainSeries
from intraday.data.ibkr import ingest_payload
from intraday.data.store import ParquetStore
from intraday.timeutils import bar_close_index, session_bounds_utc
from scripts.ingest_theta_options import (
    PER_SYMBOL_Q,
    SynthStats,
    consumer_T,
    ingest_parity_bars,
    ingest_tape,
    prev_session_oi,
    quotes_to_parity_frame,
    synthesize_chain,
    validate_same_day_expiry,
)

pytestmark = pytest.mark.swe

DAY = date(2026, 6, 8)
SIGMA_TRUE = 0.20
R = EngineConfig.default().gate.risk_free_rate     # one r everywhere (R8)
Q = PER_SYMBOL_Q["SPY"]
STRIKES = [597.0, 598.0, 599.0, 600.0, 601.0, 602.0, 603.0]


def _spot_path() -> pd.Series:
    """Deterministic intraday spot path around $600 on the 1m close grid."""
    grid = bar_close_index(DAY, "1m")
    drift = np.sin(np.linspace(0.0, 3.0, len(grid))) * 1.5
    return pd.Series(600.0 + drift, index=grid)


def _quotes_from_world(
    spot: pd.Series, *, sigma: float = SIGMA_TRUE, expiry: date = DAY,
    rel_spread: float = 0.02,
) -> pd.DataFrame:
    """Quote-bar fixture priced from the known world via vendor Black-Scholes,
    at the SAME T the consumer (and hence the inversion) uses."""
    from engine.option_pricer import black_scholes_price

    T = consumer_T(expiry, DAY)
    rows = []
    for ts, s in spot.items():
        for k in STRIKES:
            for right, ot in (("C", "call"), ("P", "put")):
                mid = black_scholes_price(float(s), k, T, R, sigma, ot, q=Q)
                if mid <= 0:
                    continue
                half = mid * rel_spread / 2.0
                rows.append({
                    "ts": ts, "expiration": expiry, "strike": k, "right": right,
                    "bid": mid - half, "ask": mid + half,
                    "bid_size": 10, "ask_size": 10, "mid": mid,
                })
    return pd.DataFrame(rows)


def _oi_for(strikes=STRIKES, *, expiry: date = DAY, value: float = 1500.0) -> pd.DataFrame:
    rows = [
        {"date": DAY - timedelta(days=1), "expiration": expiry, "strike": k,
         "right": r, "open_interest": value}
        for k in strikes for r in ("C", "P")
    ]
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def world():
    spot = _spot_path()
    quotes = _quotes_from_world(spot)
    chain, stats = synthesize_chain(
        quotes, _oi_for(), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q,
    )
    return {"spot": spot, "quotes": quotes, "chain": chain, "stats": stats}


# --------------------------------------------------------------------------- #
# Recovery: parity spot and inverted IV reproduce the constructed world
# --------------------------------------------------------------------------- #
def test_chain_has_canonical_columns(world):
    assert list(world["chain"].columns) == list(CHAIN_COLUMNS)
    assert len(world["chain"]) > 0


def test_parity_spot_recovers_true_spot(world):
    chain = world["chain"]
    spot = world["spot"]
    per_snap = chain.groupby("snapshot_ts")["spot"].first()
    for snap, s_est in per_snap.items():
        true = float(spot.asof(snap))
        assert s_est == pytest.approx(true, rel=5e-4), f"snapshot {snap}"


def test_inverted_iv_recovers_true_sigma(world):
    chain = world["chain"]
    err = (chain["implied_vol"] - SIGMA_TRUE).abs()
    atm = chain[chain["strike"] == 600.0]
    assert (atm["implied_vol"] - SIGMA_TRUE).abs().max() < 0.01
    assert float(err.median()) < 0.01


def test_reconstruction_error_does_not_grow_toward_close(world):
    """R7's specific guard: a decaying inversion T paired with the consumer's
    fixed T=1/365 would blow up near the close — error must stay flat."""
    chain = world["chain"]
    snaps = sorted(chain["snapshot_ts"].unique())
    early, late = snaps[1], snaps[-1]
    err_at = {
        s: float((chain[chain["snapshot_ts"] == s]["implied_vol"] - SIGMA_TRUE).abs().mean())
        for s in (early, late)
    }
    assert err_at[late] <= err_at[early] + 0.01


def test_available_ts_is_snapshot_plus_chain_latency(world):
    chain = world["chain"]
    lat = pd.Timedelta(milliseconds=EngineConfig.default().data.chain_latency_ms)
    chain2, _ = synthesize_chain(
        world["quotes"], _oi_for(), symbol="SPY", day=DAY, cadence="5m",
        r=R, q=Q, latency=lat,
    )
    assert ((chain2["available_ts"] - chain2["snapshot_ts"]) == lat).all()
    # And the module fixture used the 1s default — same invariant (R3).
    gaps = chain["available_ts"] - chain["snapshot_ts"]
    assert gaps.nunique() == 1


# --------------------------------------------------------------------------- #
# PIT rules: OI, past-only quotes, the no-look-ahead property
# --------------------------------------------------------------------------- #
def test_oi_is_constant_within_the_day(world):
    """R2: one EOD OI number per contract per day — never an intraday ramp."""
    per_contract = world["chain"].groupby(["expiration", "strike", "option_type"])
    assert (per_contract["open_interest"].nunique() == 1).all()
    assert (world["chain"]["open_interest"] == 1500.0).all()


def test_missing_oi_becomes_zero_not_a_guess():
    spot = _spot_path()[:60]
    quotes = _quotes_from_world(spot)
    chain, stats = synthesize_chain(
        quotes, _oi_for(strikes=[600.0]), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q,
    )
    others = chain[chain["strike"] != 600.0]
    assert (others["open_interest"] == 0.0).all()
    assert stats.oi_missing_contracts > 0
    assert any("never a guess" in w for w in stats.warnings)


def test_prev_session_oi_is_strictly_earlier(tmp_path):
    """R1: day-D snapshots may only carry OI knowable before D's open."""
    root = tmp_path / "theta"
    for d, oi_val in ((date(2026, 6, 4), 111.0), (date(2026, 6, 5), 222.0), (DAY, 999.0)):
        part = root / "ticker=SPY" / f"date={d.isoformat()}"
        part.mkdir(parents=True)
        _oi_for(value=oi_val).to_parquet(part / "oi.parquet", index=False)

    oi = prev_session_oi(root, "SPY", DAY)
    assert (oi["open_interest"] == 222.0).all()      # latest STRICTLY before D
    assert prev_session_oi(root, "SPY", date(2026, 6, 4)).empty
    assert prev_session_oi(root, "QQQ", DAY).empty


def test_no_look_ahead_property():
    """R21: appending FUTURE quotes must not change an earlier PIT slice."""
    spot = _spot_path()
    full_quotes = _quotes_from_world(spot)
    grid = bar_close_index(DAY, "5m")
    cutoff = grid[20]
    early_quotes = full_quotes[pd.to_datetime(full_quotes["ts"], utc=True) <= cutoff]

    chain_full, _ = synthesize_chain(
        full_quotes, _oi_for(), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q)
    chain_early, _ = synthesize_chain(
        early_quotes, _oi_for(), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q)

    a = chain_full[chain_full["snapshot_ts"] <= cutoff].reset_index(drop=True)
    b = chain_early[chain_early["snapshot_ts"] <= cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_stale_quotes_are_not_carried_forward():
    """A quote older than one cadence step never feeds a snapshot (R12) — the
    snapshot is dropped (absent is unknowable), not ffilled."""
    spot = _spot_path()[:30]                          # quotes stop 30 min in
    quotes = _quotes_from_world(spot)
    chain, stats = synthesize_chain(
        quotes, _oi_for(), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q)
    last_covered = spot.index[-1]
    assert (pd.to_datetime(chain["snapshot_ts"]) <= last_covered + pd.Timedelta("5min")).all()
    assert stats.snapshots_no_spot > 0


# --------------------------------------------------------------------------- #
# Honesty refusals
# --------------------------------------------------------------------------- #
def test_junk_quotes_yield_no_iv_rows():
    """R5: zero-bid / crossed quotes are not invertible — dropped, never clamped."""
    grid = bar_close_index(DAY, "1m")[:10]
    rows = []
    for ts in grid:
        rows += [
            {"ts": ts, "expiration": DAY, "strike": 600.0, "right": "C",
             "bid": 0.0, "ask": 0.50, "bid_size": 0, "ask_size": 5, "mid": np.nan},
            {"ts": ts, "expiration": DAY, "strike": 600.0, "right": "P",
             "bid": 1.20, "ask": 1.10, "bid_size": 5, "ask_size": 5, "mid": np.nan},
        ]
    chain, stats = synthesize_chain(
        pd.DataFrame(rows), _oi_for(), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q)
    assert chain.empty                                # no parity pair, no spot, no rows
    assert stats.snapshots_no_spot == stats.snapshots_total


def test_spx_synthesis_is_refused():
    """R14: AM/PM settlement ambiguity + junk SPXW OI -> refuse, never approximate."""
    with pytest.raises(ValueError, match="SPY/QQQ"):
        synthesize_chain(
            _quotes_from_world(_spot_path()[:5]), _oi_for(),
            symbol="SPX", day=DAY, cadence="5m", r=R, q=Q)
    with pytest.raises(ValueError):
        synthesize_chain(
            _quotes_from_world(_spot_path()[:5]), _oi_for(),
            symbol="SPXW", day=DAY, cadence="5m", r=R, q=Q)


def test_same_day_expiry_validator():
    """R13/G8: the engine hardcodes expiry == session day; count both tenors."""
    spot = _spot_path()[:60]
    far = DAY + timedelta(days=7)
    chain_far, _ = synthesize_chain(
        _quotes_from_world(spot, expiry=far), _oi_for(expiry=far),
        symbol="SPY", day=DAY, cadence="5m", r=R, q=Q)
    same, other = validate_same_day_expiry(chain_far, DAY)
    assert same == 0 and other > 0

    chain_0dte, _ = synthesize_chain(
        _quotes_from_world(spot), _oi_for(), symbol="SPY", day=DAY, cadence="5m", r=R, q=Q)
    same, other = validate_same_day_expiry(chain_0dte, DAY)
    assert same > 0 and other == 0
    assert validate_same_day_expiry(pd.DataFrame(columns=list(CHAIN_COLUMNS)), DAY) == (0, 0)


# --------------------------------------------------------------------------- #
# Engine consumption: store round-trip -> GEX/ATM-IV actually populate
# --------------------------------------------------------------------------- #
def test_chain_roundtrip_feeds_gamma_and_atm_iv(tmp_path, world):
    from intraday.features.gex import gamma_structure_at
    from intraday.features.vrp import atm_iv_at

    store = ParquetStore(tmp_path / "store")
    store.write_chain(OptionChainSeries("SPY", world["chain"], DataSource.THETA_DERIVED), DAY)
    chain = store.read_chain("SPY", DAY)
    assert chain.source is DataSource.THETA_DERIVED

    open_utc, _ = session_bounds_utc(DAY)
    as_of = open_utc + pd.Timedelta(minutes=90)
    structure = gamma_structure_at(chain, as_of, expiry=DAY, ticker="SPY")
    assert structure is not None
    assert structure.regime is not None
    iv = atm_iv_at(chain, as_of, expiry=DAY)
    assert iv == pytest.approx(SIGMA_TRUE, abs=0.01)


# --------------------------------------------------------------------------- #
# Tape ingest (R16)
# --------------------------------------------------------------------------- #
def test_ingest_tape_stamps_available_ts(tmp_path):
    ts0 = session_bounds_utc(DAY)[0] + pd.Timedelta(minutes=5)
    trades = pd.DataFrame([{
        "ts": ts0, "expiration": DAY, "strike": 600.0, "right": "C",
        "price": 1.15, "size": 5, "nbbo_bid": 1.10, "nbbo_ask": 1.20,
        "side_inferred": "buy",
    }])
    store = ParquetStore(tmp_path / "store")
    lat = pd.Timedelta(milliseconds=250)
    ingest_tape(store, "SPY", DAY, trades, latency=lat)

    tape = store.read_tape("SPY", DAY)
    assert tape.source is DataSource.THETA
    assert list(tape.frame.columns) == list(TAPE_COLUMNS)
    assert tape.frame.loc[0, "available_ts"] == ts0 + lat
    # PIT: the print is invisible until ts + latency has passed.
    assert tape.window_available_at(ts0 + pd.Timedelta(milliseconds=100),
                                    pd.Timedelta("10min")).empty
    assert len(tape.window_available_at(ts0 + lat, pd.Timedelta("10min"))) == 1


# --------------------------------------------------------------------------- #
# Parity bars (optional path; no-clobber invariant)
# --------------------------------------------------------------------------- #
def test_quotes_to_parity_frame_picks_min_gap_strike():
    quotes = _quotes_from_world(_spot_path()[:5])
    pframe = quotes_to_parity_frame(quotes)
    assert set(pframe.columns) == {"strike", "call_mid", "put_mid", "expiry"}
    assert len(pframe) == 5                          # one ATM pair per timestamp
    # Forward sits near 600 — the chosen strike must be the nearest one.
    assert pframe["strike"].isin([599.0, 600.0, 601.0]).all()


def test_ingest_parity_bars_never_clobbers_existing_bars(tmp_path, make_ibkr_payload):
    store = ParquetStore(tmp_path / "store")
    ingest_payload(store, "SPY", "1m", make_ibkr_payload(DAY, "1m"))
    before = store.read_bars("SPY", DAY, "1m")

    quotes = _quotes_from_world(_spot_path())
    out = ingest_parity_bars(store, "SPY", DAY, quotes,
                             interval="1m", cfg=EngineConfig.default(), q=Q)
    assert out is None                               # skipped: IBKR wins
    after = store.read_bars("SPY", DAY, "1m")
    assert after.source is DataSource.IBKR
    pd.testing.assert_frame_equal(before.frame, after.frame)


def test_ingest_parity_bars_writes_when_absent(tmp_path):
    store = ParquetStore(tmp_path / "store")
    quotes = _quotes_from_world(_spot_path())
    out = ingest_parity_bars(store, "SPY", DAY, quotes,
                             interval="1m", cfg=EngineConfig.default(), q=Q)
    assert out is not None
    bars = store.read_bars("SPY", DAY, "1m")
    assert bars.source is DataSource.PARITY
    # Recovered close tracks the true spot path.
    true = _spot_path()
    err = (bars.frame["close"] - true.reindex(bars.frame.index)).abs()
    assert float(err.max()) < 0.5


# --------------------------------------------------------------------------- #
# CLI end-to-end over a raw partition
# --------------------------------------------------------------------------- #
def _write_raw_partition(root, sym, day, *, quotes, trades, oi):
    part = root / f"ticker={sym}" / f"date={day.isoformat()}"
    part.mkdir(parents=True)
    trades.to_parquet(part / "trades.parquet", index=False)
    quotes.to_parquet(part / "quotes.parquet", index=False)
    oi.to_parquet(part / "oi.parquet", index=False)


def test_main_end_to_end(tmp_path):
    from scripts.ingest_theta_options import main

    raw = tmp_path / "theta"
    store_root = tmp_path / "store"
    spot = _spot_path()
    quotes = _quotes_from_world(spot)
    ts0 = spot.index[5]
    trades = pd.DataFrame([{
        "ts": ts0, "expiration": DAY, "strike": 600.0, "right": "C",
        "price": 1.15, "size": 5, "nbbo_bid": 1.10, "nbbo_ask": 1.20,
        "side_inferred": "buy",
    }])
    _write_raw_partition(raw, "SPY", DAY, quotes=quotes, trades=trades, oi=_oi_for())
    # A prior session carrying the PIT OI (only its oi.parquet matters).
    prev = raw / "ticker=SPY" / "date=2026-06-05"
    prev.mkdir(parents=True)
    _oi_for(value=777.0).to_parquet(prev / "oi.parquet", index=False)

    rc = main([
        "--raw-root", str(raw), "--store-root", str(store_root),
        "--symbols", "SPY", "--start", "2026-06-08", "--end", "2026-06-08",
    ])
    assert rc == 0

    store = ParquetStore(store_root)
    assert store.read_tape("SPY", DAY).source is DataSource.THETA
    frame, src = store.read_option_quotes("SPY", DAY, interval="1m")
    assert src is DataSource.THETA and len(frame) == len(quotes)
    chain = store.read_chain("SPY", DAY)
    assert chain.source is DataSource.THETA_DERIVED
    assert (chain.frame["open_interest"] == 777.0).all()  # prev-session OI, not 1500

    import json
    sidecar = json.loads(
        (store_root / "option_chain" / "ticker=SPY" / f"date={DAY}" /
         "chain_synthesis.json").read_text()
    )
    assert sidecar["synthesized"] is True
    assert sidecar["oi_policy"] == "prev_session"
    assert sidecar["rows_emitted"] > 0


def test_main_refuses_non_0dte_chain(tmp_path):
    from scripts.ingest_theta_options import main

    raw = tmp_path / "theta"
    store_root = tmp_path / "store"
    far = DAY + timedelta(days=7)
    quotes = _quotes_from_world(_spot_path()[:60], expiry=far)
    trades = quotes.head(1).assign(price=1.0, size=1, nbbo_bid=1.0, nbbo_ask=1.1,
                                   side_inferred="mid")[
        ["ts", "expiration", "strike", "right", "price", "size",
         "nbbo_bid", "nbbo_ask", "side_inferred"]
    ]
    _write_raw_partition(raw, "SPY", DAY, quotes=quotes, trades=trades,
                         oi=_oi_for(expiry=far))
    rc = main([
        "--raw-root", str(raw), "--store-root", str(store_root),
        "--symbols", "SPY", "--start", "2026-06-08", "--end", "2026-06-08",
    ])
    assert rc == 1
    assert not (store_root / "option_chain" / "ticker=SPY" / f"date={DAY}").exists()


def test_synth_stats_shape():
    stats = SynthStats()
    d = stats.as_dict()
    assert set(d) == {"snapshots_total", "snapshots_no_spot", "rows_emitted",
                      "rows_dropped_iv", "oi_missing_contracts", "warnings"}
