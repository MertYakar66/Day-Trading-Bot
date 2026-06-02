"""S1 — Gamma-regime strategy, the spine (DESIGN.md §5 S1).

Thesis: the dealer gamma regime conditions the *character* of the tape.
- **Positive net GEX** (``long_gamma_dampening``): dealers fade moves → tape
  mean-reverts. S1 FADES an extension (price stretched ``entry_z`` σ from VWAP)
  when order flow is exhausting (one-sided OFI in the stretch direction), with the
  target back at VWAP.
- **Negative net GEX** (``short_gamma_amplifying``): dealers chase moves → tape
  trends. S1 RIDES a break of the gamma-flip level when OFI confirms the breakout,
  targeting the nearest wall (next level), with a stop back at the flip.
- ``near_flip`` / ``neutral`` / unknown → stand aside (regime too unstable).

S1 trades the underlying STOCK (SPY/QQQ) — the tradable proxy. Like S3, its win
probability is the gambler's-ruin fair baseline + an explicit ``edge`` thesis, so
the gate is never rubber-stamped (DESIGN §6; see PROGRESS.md review note). The GEX,
flip, walls, and OFI all come from the PIT feature row.
"""

from __future__ import annotations

from ..config import EngineConfig
from ..contracts import AssetKind, GammaRegime, SignalProposal, Side
from ..data.provider import asset_kind_for
from ..features.base import FeatureRow

_ADV_SHARES: dict[str, float] = {"SPY": 70_000_000.0, "QQQ": 40_000_000.0}


class S1GammaRegime:
    """Dealer-gamma-conditioned mean-reversion / trend strategy."""

    strategy_id = "S1_gamma_regime"
    needs_options = True   # GEX/flip/walls (chain) + OFI (tape)
    needs_rv = False

    def __init__(
        self,
        *,
        entry_z: float = 1.5,
        stop_k: float = 1.0,
        target_k: float = 2.0,
        ofi_threshold: float = 0.30,
        edge: float = 0.10,
    ) -> None:
        self.entry_z = entry_z
        self.stop_k = stop_k
        self.target_k = target_k
        self.ofi_threshold = ofi_threshold
        self.edge = edge

    def propose(self, fr: FeatureRow, *, config: EngineConfig) -> SignalProposal | None:
        if asset_kind_for(fr.symbol) is AssetKind.INDEX:
            return None  # SPX is context; trade the underlying proxy only
        if (
            fr.gamma_regime is None
            or fr.last_price is None
            or fr.vwap is None
            or fr.vwap_sigma is None
            or fr.vwap_sigma <= 0
            or fr.vwap_dev_sigma is None
            or fr.ofi is None
        ):
            return None

        ref = fr.last_price
        sigma = fr.vwap_sigma
        z = fr.vwap_dev_sigma
        ofi = fr.ofi

        side = target = stop = None

        if fr.gamma_regime is GammaRegime.LONG_GAMMA:
            # Fade an exhausted extension back toward VWAP.
            if z >= self.entry_z and ofi >= self.ofi_threshold:
                side, target, stop = Side.SHORT, fr.vwap, ref + self.stop_k * sigma
            elif z <= -self.entry_z and ofi <= -self.ofi_threshold:
                side, target, stop = Side.LONG, fr.vwap, ref - self.stop_k * sigma

        elif fr.gamma_regime is GammaRegime.SHORT_GAMMA and fr.flip_level is not None:
            # Ride a confirmed break of the gamma-flip toward the next wall.
            if ref > fr.flip_level and ofi >= self.ofi_threshold:
                tgt = fr.nearest_call_wall if (fr.nearest_call_wall and fr.nearest_call_wall > ref) else ref + self.target_k * sigma
                side, target, stop = Side.LONG, tgt, ref - self.stop_k * sigma
            elif ref < fr.flip_level and ofi <= -self.ofi_threshold:
                tgt = fr.nearest_put_wall if (fr.nearest_put_wall and fr.nearest_put_wall < ref) else ref - self.target_k * sigma
                side, target, stop = Side.SHORT, tgt, ref + self.stop_k * sigma

        if side is None:
            return None
        # Guard valid geometry (reward and risk strictly positive on the right side).
        reward = abs(target - ref)
        risk = abs(ref - stop)
        if reward <= 0 or risk <= 0:
            return None
        if side is Side.LONG and (target <= ref or stop >= ref):
            return None
        if side is Side.SHORT and (target >= ref or stop <= ref):
            return None

        p_fair = risk / (reward + risk)
        win_prob = min(0.99, max(0.01, p_fair + self.edge))

        return SignalProposal(
            strategy_id=self.strategy_id,
            symbol=fr.symbol,
            ts=fr.as_of,
            side=side,
            instrument=AssetKind.STOCK,
            ref_price=ref,
            target_price=float(target),
            stop_price=float(stop),
            win_prob=win_prob,
            spread=config.cost.fallback_spread_pct * ref,
            adv=_ADV_SHARES.get(fr.symbol),
            meta={
                "regime": fr.gamma_regime.value, "gex_total": fr.gex_total,
                "ofi": ofi, "vwap_dev_sigma": z, "flip_level": fr.flip_level,
                "p_fair": p_fair, "edge": self.edge,
            },
        )
