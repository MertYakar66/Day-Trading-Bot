"""Conservative fill model (DESIGN.md §8).

Never fill at the price you "saw". A decision made on the bar closing at ``t``
fills at the NEXT bar's open — a one-bar reaction delay at a price we did not pick
intrabar. All frictions (spread + square-root impact + commission, from
:mod:`intraday.costs` / SWE) are accounted as an explicit *cost*, not folded into
the fill price, so price PnL (gross) and frictions (costs) stay cleanly separated
and slippage is never double-counted. ``net = gross − costs``.
"""

from __future__ import annotations

from ..config import CostConfig
from ..contracts import AssetKind, Side
from ..costs import one_way_slippage


def _trade_type(instrument: AssetKind) -> str:
    return "option" if instrument is AssetKind.OPTION else "stock"


def conservative_fill(
    *,
    side: Side,
    instrument: AssetKind,
    next_open: float,
    spread: float,
    size: int,
    adv: float | None,
    config: CostConfig,
    is_entry: bool,
) -> tuple[float, float]:
    """Return ``(fill_price, leg_cost_usd)`` for one leg.

    The fill is the conservative reference price (``next_open``); frictions are
    returned as ``leg_cost_usd`` = slippage dollars (adverse per-share move ×
    size × multiplier) + commission, so entry+exit leg costs sum to the gate's
    round-trip estimate and slippage is not double-counted in the price.
    """
    from engine import transaction_costs as tc  # lazy

    per_share = one_way_slippage(
        side=side,
        instrument=instrument,
        ref_price=next_open,
        spread=spread,
        size=size,
        adv=adv,
        config=config,
        is_entry=is_entry,
    )
    mult = instrument.multiplier
    slippage_usd = abs(per_share) * size * mult
    commission = tc.calculate_commission(_trade_type(instrument), size)
    return next_open, slippage_usd + commission
