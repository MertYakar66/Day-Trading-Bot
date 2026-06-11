"""Feature-layer contracts + point-in-time (PIT) sampling helpers.

Every feature builder here computes a *causal* series (each value uses only data
at or before its own timestamp) and is sampled at a decision time via
:func:`latest_value`, which additionally respects arrival latency. This two-layer
design makes look-ahead structurally impossible: the causal computation cannot
see the future, and the PIT sampler cannot read a value before it has "arrived".

The no-look-ahead property tests assert that appending future bars/prints never
changes any feature value sampled at an earlier ``as_of``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

import pandas as pd

from ..contracts import GammaRegime


def latest_value(
    series: pd.Series,
    as_of: pd.Timestamp,
    latency: pd.Timedelta = pd.Timedelta(0),
):
    """Last value of a ts-indexed causal ``series`` available at ``as_of``.

    A value stamped ``t`` is available at ``t + latency``; we therefore take the
    last entry with ``t <= as_of - latency``. Returns ``None`` if nothing is
    available yet.
    """
    cutoff = pd.Timestamp(as_of) - latency
    s = series.loc[series.index <= cutoff]
    if s.empty:
        return None
    val = s.iloc[-1]
    return val


@dataclass(frozen=True)
class FeatureRow:
    """All Phase-0 features at one decision timestamp (PIT-stamped).

    Any field may be ``None`` if not yet computable (e.g. the opening range before
    the OR window closes, or option features when no snapshot has arrived). The S3
    control strategy reads only the VWAP/ORB/price fields; the GEX/OFI/VRP fields
    exist for S1/S2 (Phase 1) and for transparency logging.
    """

    symbol: str
    as_of: pd.Timestamp
    last_price: float | None = None
    # VWAP block
    vwap: float | None = None
    vwap_sigma: float | None = None
    vwap_dev_sigma: float | None = None
    # Opening range
    orb_high: float | None = None
    orb_low: float | None = None
    orb_volume: float | None = None
    # Order-flow imbalance (option tape)
    ofi: float | None = None
    # Volatility. Two RV clocks, two jobs (features.realized_vol docstring):
    # rv = intraday (S2's move-to-close projection); rv_calendar = close-to-close
    # calendar clock (IV's comparator); vrp = atm_iv - rv_calendar (gate).
    rv: float | None = None
    rv_calendar: float | None = None
    atm_iv: float | None = None
    vrp: float | None = None
    # Dealer gamma structure
    gex_total: float | None = None
    gamma_regime: GammaRegime | None = None
    flip_level: float | None = None
    flip_distance_pct: float | None = None
    nearest_call_wall: float | None = None
    nearest_put_wall: float | None = None
    meta: Mapping = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(self.gamma_regime, GammaRegime):
            d["gamma_regime"] = self.gamma_regime.value
        d["as_of"] = pd.Timestamp(self.as_of).isoformat()
        return d
