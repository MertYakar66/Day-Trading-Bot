"""Per-trade stop / target helpers (DESIGN.md §7).

Stops are set at entry from an intraday-sigma distance or a structural level. The
hard intraday time-stop (flatten before the close) is computed in
:mod:`intraday.timeutils`; the daily kill-switch lives in the backtest/reviewers.
"""

from __future__ import annotations

from ..contracts import Side


def sigma_stop_target(
    ref_price: float,
    sigma: float,
    side: Side,
    *,
    stop_k: float,
    target_price: float | None = None,
    target_k: float | None = None,
) -> tuple[float, float]:
    """Return ``(stop_price, target_price)`` for a trade.

    The stop is ``stop_k·sigma`` on the adverse side of ``ref_price``. The target
    is either an explicit ``target_price`` (e.g. VWAP, for a reversion trade) or
    ``target_k·sigma`` on the favorable side.
    """
    if side is Side.LONG:
        stop = ref_price - stop_k * sigma
        tgt = target_price if target_price is not None else ref_price + (target_k or stop_k) * sigma
    else:
        stop = ref_price + stop_k * sigma
        tgt = target_price if target_price is not None else ref_price - (target_k or stop_k) * sigma
    return stop, tgt
