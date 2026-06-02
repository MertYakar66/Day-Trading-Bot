"""S5 — VWAP momentum / trend continuation (underlying-only).

The direct mirror of the S3 VWAP-reversion control: where S3 FADES a ≥``entry_z``σ
stretch from session VWAP, S5 RIDES it (trend-continuation). Running both on the
same real data is a clean test of which regime — mean-reversion or momentum — the
intraday tape actually exhibits, net of costs. Needs no options.

Honesty discipline (identical to S3/S4): ``win_prob = p_fair + edge`` over the
gambler's-ruin fair baseline; ``edge=0`` ⇒ the gate refuses everything. The realized
outcome on real data is the verdict. NOTE: because S5 is the literal opposite side
of S3, evaluating both on the same window is multiple testing — the deflated-Sharpe
penalty (intraday.eval) and a walk-forward split exist precisely to keep that honest.
"""

from __future__ import annotations

from ..config import EngineConfig
from ..contracts import AssetKind, SignalProposal, Side
from ..data.provider import asset_kind_for
from ..features.base import FeatureRow

# Per-symbol ADV (shares) for square-root market impact; mirrors S3.
_ADV_SHARES: dict[str, float] = {"SPY": 70_000_000.0, "QQQ": 40_000_000.0}


class S5VwapMomentum:
    """Ride extreme VWAP deviations (momentum). One proposal per tick; only when flat."""

    strategy_id = "S5_vwap_momentum"

    def __init__(
        self,
        *,
        entry_z: float = 2.0,
        target_k: float = 1.0,  # target = target_k σ further in the trend direction
        stop_k: float = 1.0,    # stop   = stop_k  σ against (back toward VWAP)
        edge: float = 0.10,
    ) -> None:
        self.entry_z = entry_z
        self.target_k = target_k
        self.stop_k = stop_k
        self.edge = edge

    def propose(self, fr: FeatureRow, *, config: EngineConfig) -> SignalProposal | None:
        if fr.vwap is None or fr.vwap_sigma is None or fr.last_price is None:
            return None
        if fr.vwap_dev_sigma is None or fr.orb_high is None:  # past the noisy open
            return None
        if fr.vwap_sigma <= 0:
            return None

        z = fr.vwap_dev_sigma
        if abs(z) < self.entry_z:
            return None
        kind = asset_kind_for(fr.symbol)
        if kind is AssetKind.INDEX:
            return None

        ref = fr.last_price
        sigma = fr.vwap_sigma
        if z >= self.entry_z:        # stretched ABOVE VWAP → ride further up
            side = Side.LONG
            target = ref + self.target_k * sigma
            stop = ref - self.stop_k * sigma
        else:                        # stretched BELOW VWAP → ride further down
            side = Side.SHORT
            target = ref - self.target_k * sigma
            stop = ref + self.stop_k * sigma

        reward = abs(target - ref)
        risk = abs(ref - stop)
        denom = reward + risk
        p_fair = (risk / denom) if denom > 0 else 0.5
        win_prob = min(0.99, max(0.01, p_fair + self.edge))
        spread = config.cost.fallback_spread_pct * ref

        return SignalProposal(
            strategy_id=self.strategy_id,
            symbol=fr.symbol,
            ts=fr.as_of,
            side=side,
            instrument=AssetKind.STOCK,
            ref_price=ref,
            target_price=target,
            stop_price=stop,
            win_prob=win_prob,
            spread=spread,
            adv=_ADV_SHARES.get(fr.symbol),
            meta={"vwap_dev_sigma": z, "vwap": fr.vwap, "sigma": sigma,
                  "p_fair": p_fair, "edge": self.edge},
        )
