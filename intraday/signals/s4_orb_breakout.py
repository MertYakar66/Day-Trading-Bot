"""S4 — Opening-Range Breakout (intraday momentum; underlying-only).

A classic, well-documented day-trading pattern and the momentum counterpart to the
S3 reversion control: once the opening range (first N minutes) is set, a break
ABOVE the range high is taken long (target above, stop below), a break BELOW the
range low is taken short. It needs no options — only the OR and price — so it is
fully testable on real underlying data.

Honesty discipline (identical to S3): a target/stop bet on a driftless price has a
gambler's-ruin fair win probability ``p_fair = risk/(reward+risk)`` (gross EV ≡ 0).
S4 sets ``win_prob = p_fair + edge`` where ``edge`` (default 0.10) is the EXPLICIT,
falsifiable breakout-continuation thesis ("a clean OR break continues ``edge`` more
often than chance"). ``edge=0`` ⇒ the gate refuses every trade. The realized
hit-rate on real data — not the assumed edge — is the verdict.
"""

from __future__ import annotations

from ..config import EngineConfig
from ..contracts import AssetKind, Side, SignalProposal
from ..data.provider import asset_kind_for
from ..features.base import FeatureRow

# Per-symbol ADV (shares) for square-root market impact; mirrors S3 (negligible at
# our size, but kept consistent so cost is not understated relative to S3).
_ADV_SHARES: dict[str, float] = {"SPY": 70_000_000.0, "QQQ": 40_000_000.0}


class S4OpeningRangeBreakout:
    """Opening-range breakout. One proposal per tick; only when flat."""

    strategy_id = "S4_orb_breakout"

    def __init__(
        self,
        *,
        buffer_frac: float = 0.05,  # break must clear the OR edge by buffer_frac × OR range
        target_r: float = 1.0,      # target = target_r × OR range beyond entry
        stop_r: float = 0.5,        # stop   = stop_r  × OR range against entry
        edge: float = 0.10,
    ) -> None:
        self.buffer_frac = buffer_frac
        self.target_r = target_r
        self.stop_r = stop_r
        self.edge = edge

    def propose(self, fr: FeatureRow, *, config: EngineConfig) -> SignalProposal | None:
        if fr.last_price is None or fr.orb_high is None or fr.orb_low is None:
            return None
        rng = fr.orb_high - fr.orb_low
        if rng <= 0:
            return None
        kind = asset_kind_for(fr.symbol)
        if kind is AssetKind.INDEX:  # breakout needs a tradeable underlying with volume
            return None

        ref = fr.last_price
        buf = self.buffer_frac * rng
        if ref > fr.orb_high + buf:           # upside breakout → momentum long
            side = Side.LONG
            target = ref + self.target_r * rng
            stop = ref - self.stop_r * rng
        elif ref < fr.orb_low - buf:          # downside breakout → momentum short
            side = Side.SHORT
            target = ref - self.target_r * rng
            stop = ref + self.stop_r * rng
        else:
            return None

        # Strictly positive geometry (mirrors S1) — p_fair is a true gambler's-ruin
        # probability, never a silent 0.5 fallback.
        reward = abs(target - ref)
        risk = abs(ref - stop)
        if reward <= 0 or risk <= 0:
            return None
        p_fair = risk / (reward + risk)
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
            meta={"orb_high": fr.orb_high, "orb_low": fr.orb_low, "or_range": rng,
                  "p_fair": p_fair, "edge": self.edge},
        )
