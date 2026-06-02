"""S3 — VWAP-reversion / opening-range control strategy (DESIGN.md §5 S3).

S3 is the *benchmark/control*, not an alpha claim: a simple, well-understood
mean-reversion rule used to validate the harness, cost model, and metrics. If S3
misbehaves in backtest, the harness is wrong — not the market (TASKS.md T0.8).

Rule: once the opening range has formed (so we are past the noisy open) and VWAP
bands are defined, fade extreme deviations from session VWAP back toward it:
- price ≥ ``entry_z`` σ ABOVE VWAP → SHORT, target = VWAP, stop = ``stop_k`` σ higher;
- price ≤ ``entry_z`` σ BELOW VWAP → LONG,  target = VWAP, stop = ``stop_k`` σ lower.

Honesty about the win probability (this is load-bearing — see the adversarial
review note in PROGRESS.md): a flat ``win_prob=0.5`` is NOT neutral when the
payoff is asymmetric. The genuinely no-edge baseline for a target/stop bet on a
driftless price is the **gambler's-ruin probability**
``p_fair = risk / (reward + risk)``, for which ``E[gross] = p_fair·reward −
(1−p_fair)·risk ≡ 0`` exactly. S3 therefore sets ``win_prob = p_fair + edge``,
where ``edge`` (default 0.0) is the strategy's EXPLICIT, falsifiable
mean-reversion thesis ("a ≥entry_z σ stretch reverts ``edge`` more often than
chance"). With ``edge=0`` the gate correctly REFUSES every trade (no edge cannot
beat costs); with ``edge>0`` the gate certifies EV *conditional on that stated
edge*, to be validated against realized hit-rate (DESIGN §11 #4). This keeps the
control from rubber-stamping itself through the gate.
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
        edge: float = 0.10,
    ) -> None:
        self.entry_z = entry_z
        self.stop_k = stop_k
        # Explicit, falsifiable edge over the gambler's-ruin fair baseline.
        # 0.0 ⇒ no edge claim ⇒ the gate refuses all S3 trades (E[gross]=0 < costs).
        # The default 0.10 is the documented mean-reversion thesis (calibrate on
        # real data). It is added to p_fair, NOT a free-standing win probability.
        self.edge = edge

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

        # No-edge fair baseline (gambler's ruin) + the explicit edge thesis.
        reward = abs(target - ref)
        risk = abs(ref - stop)
        denom = reward + risk
        p_fair = (risk / denom) if denom > 0 else 0.5
        win_prob = min(0.99, max(0.01, p_fair + self.edge))

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
