"""Prior-session daily real-data context, available at a session's open (PIT).

This is the thin, additive wiring that surfaces SWE's captured *daily* real data
(risk-free curve + the VIX complex) to the intraday engine. It bundles values that
are known at the prior session's close — and therefore usable from the very first
bar of the next session without any look-ahead — into a single immutable
:class:`DailyContext` that the backtester stamps onto every :class:`FeatureRow`.

Why daily, and why prior-session: the treasury curve and the VIX/VIX3M/VVIX/SKEW
complex print once per day at the close. A value stamped at the close on date
``d`` is the latest thing a decision on the *next* trading day can lawfully use;
:mod:`intraday.data.swe_offline` enforces this with ``prior_session=True`` lookups.

It is purely additive: with no provider supplied the engine behaves exactly as
before (every field ``None``, the config's scalar risk-free rate). With one
supplied, strategies/reviewers gain a real risk-free rate and a real vol-regime
read without any new feed or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..logging_config import get_logger
from .swe_offline import (
    RiskFreeCurve,
    SweDataUnavailable,
    VolRegimePanel,
    load_risk_free_curve,
    load_vol_regime,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class DailyContext:
    """Prior-session daily real-data context for one trading day.

    All fields may be ``None`` when the underlying series does not cover the day.
    Volatility-index fields are in points (``vix == 16.7`` ⇒ 16.7% annualized);
    ``risk_free_rate`` is an annualized decimal (0.036 ⇒ 3.6%).
    """

    day: date
    risk_free_rate: float | None = None
    vix: float | None = None
    vix_term_slope: float | None = None   # vix / vix3m: <1 contango, >1 backwardation
    vix_front_slope: float | None = None  # vix9d / vix: very front of the curve
    vvix: float | None = None             # vol-of-vol
    skew: float | None = None             # CBOE SKEW (tail pricing)

    @property
    def is_backwardated(self) -> bool | None:
        """True when the front of the term structure is inverted (stress regime)."""
        if self.vix_term_slope is None:
            return None
        return self.vix_term_slope > 1.0


class DailyContextProvider:
    """Produces a PIT :class:`DailyContext` per trading day from daily sources.

    Construct directly with already-loaded panels, or via :meth:`from_swe` which
    loads the SWE offline files (network-free). ``rate_tenor`` selects the treasury
    point used as the risk-free rate (``rate_3m`` suits short-dated options).
    """

    def __init__(
        self,
        *,
        risk_free_curve: RiskFreeCurve | None = None,
        vol_regime: VolRegimePanel | None = None,
        rate_tenor: str = "rate_3m",
    ) -> None:
        self.risk_free_curve = risk_free_curve
        self.vol_regime = vol_regime
        self.rate_tenor = rate_tenor

    @classmethod
    def from_swe(
        cls,
        *,
        root: str | None = None,
        rate_tenor: str = "rate_3m",
        require: bool = False,
    ) -> DailyContextProvider:
        """Load the SWE risk-free curve + vol-regime panel from disk.

        With ``require=False`` (default) a missing source degrades to ``None`` for
        that slice rather than raising, so a partial SWE checkout still yields a
        usable provider. With ``require=True`` any missing source raises.
        """
        rfc = vrp = None
        try:
            rfc = load_risk_free_curve(root)
        except SweDataUnavailable:
            if require:
                raise
            logger.warning("risk-free curve unavailable; risk_free_rate will be None")
        try:
            vrp = load_vol_regime(root)
        except SweDataUnavailable:
            if require:
                raise
            logger.warning("vol-regime panel unavailable; vol fields will be None")
        return cls(risk_free_curve=rfc, vol_regime=vrp, rate_tenor=rate_tenor)

    def context_for(self, day: date) -> DailyContext:
        """The prior-session daily context for ``day`` (never look-ahead)."""
        rate = None
        if self.risk_free_curve is not None:
            try:
                rate = self.risk_free_curve.rate_on(day, self.rate_tenor)
            except SweDataUnavailable:
                rate = None
        vix = term = front = vvix = skew = None
        if self.vol_regime is not None:
            vix = self.vol_regime.vix(day)
            term = self.vol_regime.term_slope(day)
            front = self.vol_regime.front_slope(day)
            vvix = self.vol_regime.vvix(day)
            skew = self.vol_regime.skew(day)
        return DailyContext(
            day=day,
            risk_free_rate=rate,
            vix=vix,
            vix_term_slope=term,
            vix_front_slope=front,
            vvix=vvix,
            skew=skew,
        )
