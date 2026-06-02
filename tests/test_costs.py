"""Transaction-cost model correctness (DESIGN §6/§7), over SWE transaction_costs."""

from __future__ import annotations

import pytest

from intraday.config import CostConfig
from intraday.contracts import AssetKind, Side
from intraday.costs import one_way_slippage, round_trip_cost

pytestmark = pytest.mark.cost_model


def test_round_trip_components_sum_to_total():
    cb = round_trip_cost(
        side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
        spread=0.4, size=100, adv=70_000_000, config=CostConfig(),
    )
    assert cb.total == pytest.approx(cb.entry_slippage + cb.exit_slippage + cb.commission)
    assert cb.total > 0


def test_cost_monotonic_in_spread():
    base = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                           spread=0.10, size=100, adv=70_000_000)
    wider = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                            spread=0.80, size=100, adv=70_000_000)
    assert wider.total > base.total


def test_cost_monotonic_in_size():
    small = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                            spread=0.4, size=100, adv=1_000_000)
    big = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                          spread=0.4, size=10_000, adv=1_000_000)
    # Spread cost scales linearly with size; sqrt impact adds more — strictly more.
    assert big.total > 100 * small.commission  # sanity
    assert big.total / big.detail["size"] > small.total / small.detail["size"]


def test_option_multiplier_vs_stock():
    """An option leg costs ~100x a stock leg at the same per-share inputs (+comm)."""
    stock = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=10.0,
                            spread=0.10, size=1, adv=None)
    option = round_trip_cost(side=Side.LONG, instrument=AssetKind.OPTION, ref_price=10.0,
                             spread=0.10, size=1, adv=None)
    # Option has 100x slippage + nonzero commission; stock has 0 commission.
    assert option.commission > 0
    assert stock.commission == 0
    assert option.entry_slippage == pytest.approx(stock.entry_slippage * 100)


def test_impact_zero_without_adv():
    """Square-root impact is off when no ADV is supplied (spread-only)."""
    cb = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                         spread=0.4, size=100, adv=None)
    assert cb.impact == pytest.approx(0.0)


def test_impact_positive_with_adv():
    cb = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                         spread=0.4, size=50_000, adv=100_000)
    assert cb.impact > 0


def test_one_way_slippage_sign():
    buy = one_way_slippage(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                           spread=0.4, size=100, adv=None, config=CostConfig(), is_entry=True)
    sell = one_way_slippage(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                            spread=0.4, size=100, adv=None, config=CostConfig(), is_entry=False)
    assert buy > 0  # buys pay up
    assert sell < 0  # sells receive less


def test_fallback_spread_used_when_missing():
    """Zero/None spread falls back to a configured fraction of price (not free)."""
    cb = round_trip_cost(side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
                         spread=0.0, size=100, adv=None, config=CostConfig())
    assert cb.total > 0
    assert cb.detail["effective_spread"] == pytest.approx(CostConfig().fallback_spread_pct * 750.0)
