"""Unit tests for the S3 VWAP-reversion / opening-range control strategy.

S3 is the benchmark/control strategy (DESIGN.md S5). These tests pin its
``propose`` decision geometry against hand-built :class:`FeatureRow` inputs:
direction selection, target/stop placement, the gating preconditions that yield
``None``, and the spread / ADV bookkeeping on the emitted proposal.

Pure functions only -- no provider, no network, no SWE imports.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.contracts import AssetKind, Side
from intraday.features.base import FeatureRow
from intraday.signals.s3_vwap_orb import S3VwapOrb

pytestmark = pytest.mark.gate

# A fixed decision timestamp inside the RTH session (UTC). Value is irrelevant to
# the geometry; it just has to be a valid tz-aware Timestamp that round-trips.
AS_OF = pd.Timestamp("2026-05-18 15:00:00", tz="UTC")


def _row(
    symbol: str = "SPY",
    *,
    last_price: float | None = 500.0,
    vwap: float | None = 498.0,
    vwap_sigma: float | None = 0.5,
    vwap_dev_sigma: float | None = 2.0,
    orb_high: float | None = 501.0,
    orb_low: float | None = 497.0,
) -> FeatureRow:
    """Build a FeatureRow exposing exactly the fields S3 reads."""
    return FeatureRow(
        symbol=symbol,
        as_of=AS_OF,
        last_price=last_price,
        vwap=vwap,
        vwap_sigma=vwap_sigma,
        vwap_dev_sigma=vwap_dev_sigma,
        orb_high=orb_high,
        orb_low=orb_low,
        meta={},
    )


# --------------------------------------------------------------------------- #
# Direction + geometry
# --------------------------------------------------------------------------- #


def test_short_when_stretched_above_vwap(config):
    """dev_sigma >= +entry_z fades short: target == VWAP (below ref),
    stop == ref + stop_k*sigma (above ref)."""
    s3 = S3VwapOrb()  # entry_z=2.0, stop_k=1.0
    ref, vwap, sigma = 500.0, 498.0, 0.5
    fr = _row(last_price=ref, vwap=vwap, vwap_sigma=sigma, vwap_dev_sigma=2.5)

    prop = s3.propose(fr, config=config)

    assert prop is not None
    assert prop.side is Side.SHORT
    assert prop.instrument is AssetKind.STOCK
    assert prop.ref_price == pytest.approx(ref)
    # Target is VWAP and lies below the reference (we fade back down toward it).
    assert prop.target_price == pytest.approx(vwap)
    assert prop.target_price < prop.ref_price
    # Stop is one sigma above the reference for a short.
    assert prop.stop_price == pytest.approx(ref + 1.0 * sigma)
    assert prop.stop_price > prop.ref_price
    # Reward/risk geometry (per-unit, gross dollars).
    assert prop.reward_per_unit == pytest.approx(abs(vwap - ref))
    assert prop.risk_per_unit == pytest.approx(1.0 * sigma)
    # win_prob = gambler's-ruin fair baseline (risk/(reward+risk)) + default edge
    # (0.10). Here reward=2.0, risk=0.5 -> p_fair=0.2, win_prob=0.30.
    assert prop.win_prob == pytest.approx(0.5 / 2.5 + 0.10)
    assert prop.meta["p_fair"] == pytest.approx(0.20)
    assert prop.ts == AS_OF
    assert prop.meta["vwap_dev_sigma"] == pytest.approx(2.5)
    assert prop.meta["sigma"] == pytest.approx(sigma)


def test_long_when_stretched_below_vwap(config):
    """dev_sigma <= -entry_z fades long: target == VWAP (above ref),
    stop == ref - stop_k*sigma (below ref)."""
    s3 = S3VwapOrb()
    ref, vwap, sigma = 496.0, 498.0, 0.5
    fr = _row(last_price=ref, vwap=vwap, vwap_sigma=sigma, vwap_dev_sigma=-2.5)

    prop = s3.propose(fr, config=config)

    assert prop is not None
    assert prop.side is Side.LONG
    assert prop.instrument is AssetKind.STOCK
    assert prop.target_price == pytest.approx(vwap)
    assert prop.target_price > prop.ref_price  # fade back up toward VWAP
    assert prop.stop_price == pytest.approx(ref - 1.0 * sigma)
    assert prop.stop_price < prop.ref_price
    assert prop.risk_per_unit == pytest.approx(1.0 * sigma)


def test_stop_k_scales_stop_distance(config):
    """A non-default stop_k widens the stop by exactly that many sigmas."""
    s3 = S3VwapOrb(stop_k=2.0)
    ref, sigma = 500.0, 0.5
    fr = _row(last_price=ref, vwap=498.0, vwap_sigma=sigma, vwap_dev_sigma=3.0)

    prop = s3.propose(fr, config=config)

    assert prop is not None
    assert prop.stop_price == pytest.approx(ref + 2.0 * sigma)
    assert prop.risk_per_unit == pytest.approx(2.0 * sigma)


# --------------------------------------------------------------------------- #
# Entry threshold boundary
# --------------------------------------------------------------------------- #


def test_no_signal_when_deviation_below_entry_z(config):
    """|dev| strictly under entry_z -> no proposal."""
    s3 = S3VwapOrb()
    assert s3.propose(_row(vwap_dev_sigma=1.99), config=config) is None
    assert s3.propose(_row(vwap_dev_sigma=-1.99), config=config) is None
    assert s3.propose(_row(vwap_dev_sigma=0.0), config=config) is None


def test_signal_exactly_at_entry_z_threshold(config):
    """dev_sigma == entry_z is inclusive on both sides (abs(z) < entry_z is the
    only rejecting condition)."""
    s3 = S3VwapOrb()
    short = s3.propose(_row(vwap_dev_sigma=2.0), config=config)
    long = s3.propose(_row(last_price=496.0, vwap_dev_sigma=-2.0), config=config)
    assert short is not None and short.side is Side.SHORT
    assert long is not None and long.side is Side.LONG


def test_custom_entry_z_threshold(config):
    """A higher entry_z raises the bar: 2.5 sigma no longer triggers at z=3.0."""
    s3 = S3VwapOrb(entry_z=3.0)
    assert s3.propose(_row(vwap_dev_sigma=2.5), config=config) is None
    assert s3.propose(_row(vwap_dev_sigma=3.0), config=config) is not None


# --------------------------------------------------------------------------- #
# Missing / invalid feature preconditions -> None
# --------------------------------------------------------------------------- #


def test_none_when_vwap_missing(config):
    s3 = S3VwapOrb()
    assert s3.propose(_row(vwap=None), config=config) is None


def test_none_when_vwap_sigma_missing(config):
    s3 = S3VwapOrb()
    assert s3.propose(_row(vwap_sigma=None), config=config) is None


def test_none_when_last_price_missing(config):
    s3 = S3VwapOrb()
    assert s3.propose(_row(last_price=None), config=config) is None


def test_none_when_dev_sigma_missing(config):
    s3 = S3VwapOrb()
    assert s3.propose(_row(vwap_dev_sigma=None), config=config) is None


def test_none_when_orb_high_missing(config):
    """Opening range must have formed (orb_high known) before S3 acts."""
    s3 = S3VwapOrb()
    assert s3.propose(_row(orb_high=None), config=config) is None


def test_none_when_sigma_non_positive(config):
    """A degenerate (zero/negative) band sigma is not tradeable."""
    s3 = S3VwapOrb()
    assert s3.propose(_row(vwap_sigma=0.0), config=config) is None
    assert s3.propose(_row(vwap_sigma=-1.0), config=config) is None


# --------------------------------------------------------------------------- #
# Index symbols are context only -> None
# --------------------------------------------------------------------------- #


def test_none_for_index_symbol_spx(config):
    """SPX is an INDEX; S3 only trades the underlying stock, so no proposal even
    when the stretch is extreme."""
    s3 = S3VwapOrb()
    fr = _row(symbol="SPX", vwap_dev_sigma=4.0)
    assert s3.propose(fr, config=config) is None


# --------------------------------------------------------------------------- #
# Spread + ADV bookkeeping on the proposal
# --------------------------------------------------------------------------- #


def test_spread_equals_fallback_pct_times_ref(config):
    """spread == config.cost.fallback_spread_pct * ref_price."""
    s3 = S3VwapOrb()
    ref = 500.0
    prop = s3.propose(_row(last_price=ref, vwap_dev_sigma=2.5), config=config)
    assert prop is not None
    expected = config.cost.fallback_spread_pct * ref
    assert prop.spread == pytest.approx(expected)
    # Sanity on the configured default: 5 bps of a $500 price = $0.25.
    assert prop.spread == pytest.approx(0.25)


def test_adv_set_for_spy(config):
    """SPY carries its ADV estimate (70M shares) for square-root impact."""
    s3 = S3VwapOrb()
    prop = s3.propose(_row(symbol="SPY", vwap_dev_sigma=2.5), config=config)
    assert prop is not None
    assert prop.adv == pytest.approx(70_000_000.0)


def test_adv_none_for_symbol_without_estimate(config):
    """A stock symbol with no ADV table entry leaves adv unset (None)."""
    s3 = S3VwapOrb()
    prop = s3.propose(_row(symbol="IWM", vwap_dev_sigma=2.5), config=config)
    assert prop is not None  # IWM defaults to STOCK
    assert prop.symbol == "IWM"
    assert prop.adv is None


@pytest.mark.swe
def test_edge_zero_is_a_fair_bet_and_gate_blocks(config):
    """With edge=0 the win_prob is the gambler's-ruin fair value, so gross EV is
    exactly 0 and the gate must BLOCK once costs are subtracted (no rubber-stamp)."""
    from intraday.authority.gate import ExpectancyGate
    from intraday.contracts import Verdict

    s3 = S3VwapOrb(edge=0.0)
    prop = s3.propose(_row(last_price=500.0, vwap=498.0, vwap_sigma=0.5, vwap_dev_sigma=2.5), config=config)
    assert prop is not None
    res = ExpectancyGate(config).evaluate(prop, size=100)
    assert res.ev_gross == pytest.approx(0.0, abs=1e-6)
    assert res.ev_net < 0  # costs make a fair bet negative
    assert res.verdict is Verdict.BLOCKED
