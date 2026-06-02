"""Net-of-cost transaction modelling (DESIGN.md §6/§7), over SWE transaction_costs.

This is the cost authority for the expectancy gate. It computes the *round-trip*
dollar cost of a contemplated trade (entry + exit) using
``engine.transaction_costs.calculate_slippage`` for spread-based slippage **plus**
Almgren-Chriss square-root market impact, and ``calculate_commission`` for fees.

Critical units (see ``docs/SWE_API_REFERENCE.md``):
- ``calculate_slippage`` returns a POSITIVE *per-share* dollar value; we apply the
  contract multiplier ourselves (×100 for options, ×1 for stock). The SWE
  aggregate helpers (``calculate_total_entry_cost`` etc.) hard-code ×100 and never
  pass size/ADV, so they are WRONG for stock and omit impact — we deliberately do
  not use them.
- ``size`` and ``adv`` must be in the SAME units (shares for stock, contracts for
  options); the sqrt-impact term depends only on their ratio.

Conservative by design: costs are subtracted from gross expectancy; we never
understate them. When no quoted spread is available we fall back to a configured
fraction of price rather than assuming zero spread.
"""

from __future__ import annotations

from .config import CostConfig
from .contracts import AssetKind, CostBreakdown, Side


def _trade_type(instrument: AssetKind) -> str:
    return "option" if instrument is AssetKind.OPTION else "stock"


def round_trip_cost(
    *,
    side: Side,
    instrument: AssetKind,
    ref_price: float,
    spread: float,
    size: int,
    adv: float | None = None,
    open_interest: int | None = None,
    config: CostConfig | None = None,
) -> CostBreakdown:
    """Round-trip (entry + exit) cost in dollars for a contemplated trade.

    ``side`` is the *entry* direction (LONG buys to open / sells to close; SHORT
    the reverse). Both legs incur spread + impact slippage; commissions apply per
    leg. Returns a :class:`CostBreakdown` whose ``total`` is what the expectancy
    gate subtracts from gross EV.
    """
    cfg = config or CostConfig()
    from engine import transaction_costs as tc  # lazy (heavy engine package init)

    if size <= 0:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, {"reason": "non_positive_size"})

    multiplier = instrument.multiplier
    eff_spread = spread if spread and spread > 0 else cfg.fallback_spread_pct * ref_price

    entry_dir = "buy" if side is Side.LONG else "sell"
    exit_dir = "sell" if side is Side.LONG else "buy"

    def _slip(direction: str, with_impact: bool) -> float:
        return tc.calculate_slippage(
            mid_price=ref_price,
            bid_ask_spread=eff_spread,
            trade_direction=direction,
            open_interest=open_interest,
            num_contracts=size,
            adv_contracts=(int(adv) if (adv and adv > 0) else None),
            use_sqrt_impact=(with_impact and cfg.use_sqrt_impact),
            impact_coefficient=cfg.impact_coefficient,
        )

    # Per-share slippage with and without the size-impact term, both legs.
    entry_full = _slip(entry_dir, True)
    exit_full = _slip(exit_dir, True)
    entry_spread_only = _slip(entry_dir, False)
    exit_spread_only = _slip(exit_dir, False)

    units = size * multiplier
    entry_slip_usd = entry_full * units
    exit_slip_usd = exit_full * units
    impact_usd = (
        (entry_full - entry_spread_only) + (exit_full - exit_spread_only)
    ) * units

    commission = tc.calculate_commission(_trade_type(instrument), size) * 2.0  # both legs

    total = entry_slip_usd + exit_slip_usd + commission
    return CostBreakdown(
        entry_slippage=entry_slip_usd,
        exit_slippage=exit_slip_usd,
        commission=commission,
        impact=impact_usd,
        total=total,
        detail={
            "per_share_entry_slip": entry_full,
            "per_share_exit_slip": exit_full,
            "effective_spread": eff_spread,
            "multiplier": multiplier,
            "size": size,
        },
    )


def one_way_slippage(
    *,
    side: Side,
    instrument: AssetKind,
    ref_price: float,
    spread: float,
    size: int,
    adv: float | None = None,
    open_interest: int | None = None,
    config: CostConfig | None = None,
    is_entry: bool = True,
) -> float:
    """Per-share signed slippage applied to a fill price (the conservative-fill
    helper for the backtest): a buy fills *above* mid, a sell *below* mid.

    Returns a signed per-share dollar adjustment to add to the reference price.
    """
    cfg = config or CostConfig()
    from engine import transaction_costs as tc  # lazy

    if size <= 0:
        return 0.0
    eff_spread = spread if spread and spread > 0 else cfg.fallback_spread_pct * ref_price
    # Determine this leg's direction.
    if is_entry:
        direction = "buy" if side is Side.LONG else "sell"
    else:
        direction = "sell" if side is Side.LONG else "buy"
    per_share = tc.calculate_slippage(
        mid_price=ref_price,
        bid_ask_spread=eff_spread,
        trade_direction=direction,
        open_interest=open_interest,
        num_contracts=size,
        adv_contracts=(int(adv) if (adv and adv > 0) else None),
        use_sqrt_impact=cfg.use_sqrt_impact,
        impact_coefficient=cfg.impact_coefficient,
    )
    # Adverse direction: buys pay up, sells receive less.
    sign = 1.0 if direction == "buy" else -1.0
    return sign * per_share
