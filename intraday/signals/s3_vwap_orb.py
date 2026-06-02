"""S3 — VWAP-reversion / opening-range control strategy (DESIGN.md §5 S3).

S3 is the *benchmark/control*, not an alpha claim: a simple, well-understood
mean-reversion rule used to validate the harness, cost model, and metrics. If S3
misbehaves in backtest, the harness is wrong — not the market (TASKS.md T0.8).

Rule: once the opening range has formed (so we are past the noisy open) and VWAP
bands are defined, fade extreme deviations from session VWAP back toward it:
- price ≥ ``entry_z`` σ ABOVE VWAP → SHORT, target = VWAP, stop = ``stop_k`` σ higher;
- price ≤ ``entry_z`` σ BELOW VWAP → LONG,  target = VWAP, stop = ``stop_k`` σ lower.

Honesty about the win probability: ``win_prob`` is a deliberately NEUTRAL prior
(default 0.50) — the control makes no predictive claim. Its expectancy comes from
the favorable reward/risk geometry of fading a ≥2σ stretch toward VWAP, and the
gate then decides tradeability NET OF COSTS. ``win_prob`` is the obvious knob to
calibrate against realized hit-rate once real data exists (DESIGN §11 #4).
"""

from __future__ import annotations

from ..config import EngineConfig
from ..contracts import AssetKind, SignalProposal, Side
from ..data.provider import asset_kind_for
from ..features.base import FeatureRow

# Per-symbol average daily volume estimates (shares), for square-root market
# impact in the cost model. Order-of-magnitude; impact is negligible at our size.
_ADV_SHARES: dict[str, float] = {"SPY": 70_000_000.0, "QQQ": 40_000_000.0}


class S3VwapOrb:
    """VWAP-reversion control. One proposal per tick; only when flat (enforced by
    the backtest's concurrency cap)."""

    strategy_id = "S3_vwap_orb"

    def __init__(
        self,
        *,
        entry_z: float = 2.0,
        stop_k: float = 1.0,
        win_prob: float = 0.50,
    ) -> None:
        self.entry_z = entry_z
        self.stop_k = stop_k
        self.win_prob = win_prob

    def propose(self, fr: FeatureRow, *, config: EngineConfig) -> SignalProposal | None:
        # Require a defined VWAP band, a formed opening range, and a price.
        if fr.vwap is None or fr.vwap_sigma is None or fr.last_price is None:
            return None
        if fr.vwap_dev_sigma is None or fr.orb_high is None:
            return None
        if fr.vwap_sigma <= 0:
            return None

        z = fr.vwap_dev_sigma
        if abs(z) < self.entry_z:
            return None

        ref = fr.last_price
        sigma = fr.vwap_sigma
        kind = asset_kind_for(fr.symbol)
        # Trade the underlying (stock) for the control; index symbols are context.
        if kind is AssetKind.INDEX:
            return None
        spread = config.cost.fallback_spread_pct * ref

        if z >= self.entry_z:  # stretched above VWAP → fade short
            side = Side.SHORT
            target = fr.vwap
            stop = ref + self.stop_k * sigma
        else:  # stretched below VWAP → fade long
            side = Side.LONG
            target = fr.vwap
            stop = ref - self.stop_k * sigma

        return SignalProposal(
            strategy_id=self.strategy_id,
            symbol=fr.symbol,
            ts=fr.as_of,
            side=side,
            instrument=AssetKind.STOCK,
            ref_price=ref,
            target_price=target,
            stop_price=stop,
            win_prob=self.win_prob,
            spread=spread,
            adv=_ADV_SHARES.get(fr.symbol),
            meta={"vwap_dev_sigma": z, "vwap": fr.vwap, "sigma": sigma},
        )
