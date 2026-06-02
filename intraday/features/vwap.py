"""Session VWAP + volatility bands (DESIGN.md §4).

VWAP is a running cumulative statistic, so it is causal by construction: the value
at bar ``t`` uses only bars up to and including ``t``. Bands are the
volume-weighted standard deviation of typical price around VWAP, computed the same
causal way. The S3 control strategy trades the standardized deviation
``vwap_dev_sigma = (close - vwap) / sigma``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def vwap_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """Return a causal VWAP frame indexed like ``bars`` with columns
    ``vwap``, ``vwap_sigma``, ``vwap_dev_sigma``.

    ``bars`` must hold ``high``, ``low``, ``close``, ``volume``. If total volume is
    zero (e.g. an index instrument), equal weights are used so VWAP degrades to a
    cumulative mean of typical price rather than dividing by zero.
    """
    typ = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vol = bars["volume"].astype(float).clip(lower=0.0)
    if float(vol.sum()) == 0.0:
        vol = pd.Series(1.0, index=bars.index)

    cum_v = vol.cumsum()
    cum_pv = (typ * vol).cumsum()
    vwap = cum_pv / cum_v
    cum_pv2 = (typ * typ * vol).cumsum()
    var = (cum_pv2 / cum_v) - vwap**2
    sigma = np.sqrt(var.clip(lower=0.0))

    out = pd.DataFrame(index=bars.index)
    out["vwap"] = vwap
    out["vwap_sigma"] = sigma
    # Standardized deviation of the bar close from VWAP (NaN until sigma > 0).
    safe_sigma = sigma.where(sigma > 0)
    out["vwap_dev_sigma"] = (bars["close"] - vwap) / safe_sigma
    return out


def vwap_bands(vwap: float, sigma: float, k: float) -> tuple[float, float]:
    """Upper/lower VWAP band at ``±k·sigma``."""
    return vwap + k * sigma, vwap - k * sigma
