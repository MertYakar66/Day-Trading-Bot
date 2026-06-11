"""Direction-signed order-flow imbalance (OFI) from the option tape (DESIGN §4).

Each print is signed by WHAT the customer initiation means for the underlying,
not merely that it was customer-initiated (``side_inferred`` follows the SWE
tape convention: "buy" = customer buy-initiated = dealer sells):

- customer **buys calls**  or **sells puts**  → bullish underlying pressure (+)
- customer **sells calls** or **buys puts**   → bearish underlying pressure (−)
- "mid" (ambiguous aggressor)                 → excluded

``OFI = (bull_vol - bear_vol) / (bull_vol + bear_vol)`` over a rolling window of
PIT-filtered prints; OFI ∈ [-1, 1], positive = net bullish pressure — the sign
S1's entry conditions assume. (The previous sign-blind definition lumped calls
and puts, so heavy put BUYING — bearish — read as positive "buying pressure";
an audit-confirmed defect.)
"""

from __future__ import annotations

import pandas as pd

from ..contracts import OptionTape


def ofi_from_prints(prints: pd.DataFrame) -> float | None:
    """Direction-signed OFI over a set of tape prints (already PIT-filtered).

    ``None`` when there are no classifiable prints (empty, all-"mid", or zero
    classified volume) — absent is unknowable, never approximated.
    """
    if prints is None or prints.empty:
        return None
    side = prints["side_inferred"].astype(str)
    right = prints["right"].astype(str).str.strip().str.upper().str[0]
    size = prints["size"].astype(float)
    bull = ((side == "buy") & (right == "C")) | ((side == "sell") & (right == "P"))
    bear = ((side == "sell") & (right == "C")) | ((side == "buy") & (right == "P"))
    bull_vol = float(size[bull].sum())
    bear_vol = float(size[bear].sum())
    denom = bull_vol + bear_vol
    if denom <= 0:
        return None
    return (bull_vol - bear_vol) / denom


def ofi_at(
    tape: OptionTape,
    as_of: pd.Timestamp,
    lookback: pd.Timedelta = pd.Timedelta(minutes=5),
) -> float | None:
    """OFI over prints available at ``as_of`` within ``lookback`` (no look-ahead)."""
    prints = tape.window_available_at(as_of, lookback)
    return ofi_from_prints(prints)
