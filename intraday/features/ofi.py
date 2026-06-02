"""Order-flow imbalance (OFI) from the option tape's ``side_inferred`` (DESIGN §4).

``OFI = (buy_vol - sell_vol) / (buy_vol + sell_vol)`` over a rolling window of
prints. ``side_inferred`` follows the SWE tape convention: "buy" = customer
buy-initiated (dealer sells), "sell" = customer sell-initiated, "mid" = ambiguous
(excluded). OFI ∈ [-1, 1]; positive = net buying pressure.
"""

from __future__ import annotations

import pandas as pd

from ..contracts import OptionTape


def ofi_from_prints(prints: pd.DataFrame) -> float | None:
    """OFI over a set of tape prints (already PIT-filtered). ``None`` if empty/flat."""
    if prints is None or prints.empty:
        return None
    side = prints["side_inferred"]
    size = prints["size"].astype(float)
    buy_vol = float(size[side == "buy"].sum())
    sell_vol = float(size[side == "sell"].sum())
    denom = buy_vol + sell_vol
    if denom <= 0:
        return None
    return (buy_vol - sell_vol) / denom


def ofi_at(
    tape: OptionTape,
    as_of: pd.Timestamp,
    lookback: pd.Timedelta = pd.Timedelta(minutes=5),
) -> float | None:
    """OFI over prints available at ``as_of`` within ``lookback`` (no look-ahead)."""
    prints = tape.window_available_at(as_of, lookback)
    return ofi_from_prints(prints)
