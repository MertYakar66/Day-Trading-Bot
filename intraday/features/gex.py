"""Dealer GEX / gamma-flip / walls (DESIGN.md §4), wrapping SWE dealer_positioning.

This is the "gamma spine" feature. We take the most recent PIT-available chain
snapshot and hand it to ``engine.dealer_positioning.DealerPositioningAnalyzer``,
which returns a ``MarketStructure`` (signed dollar GEX, gamma-flip level, put/call
walls, regime). Sign convention (load-bearing): positive net GEX ⇒ dealers long
gamma ⇒ vol-dampening / mean-reverting tape; negative ⇒ short gamma ⇒ trending.

``engine.dealer_positioning`` is imported lazily so the rest of the feature layer
works without the SWE dependency present.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..contracts import GammaRegime, OptionChainSeries


@dataclass(frozen=True)
class GammaStructure:
    as_of: pd.Timestamp
    spot: float
    gex_total: float
    regime: GammaRegime
    confidence: float
    flip_level: float | None
    flip_distance_pct: float | None
    nearest_call_wall: float | None
    nearest_put_wall: float | None
    regime_multiplier: float


def gamma_structure_at(
    chain: OptionChainSeries,
    as_of: pd.Timestamp,
    *,
    expiry: date,
    ticker: str,
    risk_free_rate: float = 0.04,
) -> GammaStructure | None:
    """Compute the dealer gamma structure from the latest snapshot available at
    ``as_of``. Returns ``None`` if no snapshot has arrived yet.
    """
    snap = chain.latest_available(as_of)
    if snap.empty:
        return None

    from engine.dealer_positioning import (  # lazy
        DealerPositioningAnalyzer,
        dealer_regime_multiplier,
    )

    spot = float(snap["spot"].iloc[0])
    cdf = snap[["strike", "option_type", "open_interest", "implied_vol"]].copy()
    analyzer = DealerPositioningAnalyzer(risk_free_rate=risk_free_rate)
    # The analyzer wants a naive datetime (it strips tz and uses .date() for T).
    as_of_naive = pd.Timestamp(as_of).tz_convert("UTC").tz_localize(None).to_pydatetime()
    ms = analyzer.analyze(
        chain=cdf, spot=spot, expiry=expiry, ticker=ticker, as_of=as_of_naive
    )

    return GammaStructure(
        as_of=pd.Timestamp(as_of),
        spot=spot,
        gex_total=float(ms.gex_total),
        regime=GammaRegime(ms.regime),
        confidence=float(ms.confidence),
        flip_level=ms.flip_level,
        flip_distance_pct=ms.flip_distance_pct,
        nearest_call_wall=(ms.nearest_call_wall.strike if ms.nearest_call_wall else None),
        nearest_put_wall=(ms.nearest_put_wall.strike if ms.nearest_put_wall else None),
        regime_multiplier=float(dealer_regime_multiplier(ms)),
    )
