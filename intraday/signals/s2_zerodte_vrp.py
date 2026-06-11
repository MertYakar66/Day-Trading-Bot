"""S2 — 0DTE volatility relative-value, DEFINED-RISK ONLY (DESIGN.md §5 S2).

Thesis: 0DTE implied vol is frequently rich vs realized vol measured ON THE
SAME CLOCK, conditional on the dealer-gamma regime. When VRP
(= ATM IV − **calendar-clock** close-to-close RV; see ``features.vrp``) is
richly positive AND the regime is positive-gamma (vol-suppressing /
mean-reverting), S2 SELLS premium via a **defined-risk short iron condor**
(short strikes ±k·σ, long wings beyond) — never naked short gamma; the wings
cap the loss.

The clock split is deliberate and load-bearing:
- The **gate** (``fr.vrp``) compares IV against calendar-clock RV — comparing
  against intraday RTH-only RV carried a measured ~ +18.7 vol-pt bias that
  made the pilot's "+VRP" ~98% artifact (S2 must not trade an artifact).
- The **win-probability** (``sigma_real`` below) projects the remaining
  move-to-close from the INTRADAY clock (``fr.rv``) — correct for a position
  that dies at today's close, before any overnight variance can realize.

CALIBRATION NOTE: ``vrp_threshold`` (0.02) is a conservative placeholder. The
honest threshold needs real 0DTE ATM-IV history (Theta-gated); on real
single-name data the same-clock VRP mean measured ~ −0.46 vol pts, so at this
threshold S2 is expected to trade RARELY on real data until calibrated. That
is by design — standing aside beats trading a clock artifact.

Honesty (same discipline as S1/S3, see PROGRESS.md):
- The structure is priced from the chain's ATM IV via Black-Scholes (SWE
  ``option_pricer``), giving a credit and a capped max loss.
- ``win_prob`` is the **fair baseline** ``p_fair = max_loss/(credit+max_loss)``
  (which makes gross EV exactly 0 under fair pricing — no free edge) PLUS the
  genuine VRP edge: the difference between realized and implied
  ``P(settle inside the short strikes)``. At VRP=0 the edge is 0 and the gate
  refuses the trade once costs bite.
- The 4-leg round-trip cost is computed explicitly (``cost_override``) so the
  expectancy gate never understates a multi-leg structure's frictions.

Settlement: held to near the 0DTE close; the backtest settles it binary (keep
credit if the underlying is inside the short strikes, else the capped max loss —
which conservatively *overstates* loss in the short-to-long zone). Event days are
vetoed by the downgrade-only event-lockout reviewer (DESIGN §6).
"""

from __future__ import annotations

import math

from ..config import EngineConfig
from ..contracts import AssetKind, CostBreakdown, Side, SignalProposal
from ..costs import round_trip_cost
from ..features.base import FeatureRow
from ..timeutils import session_bounds_utc

# Trading-time annualization (matches the intraday RV clock): 6.5h × 252 days.
_TRADING_YEAR_SECONDS = 252.0 * 6.5 * 3600.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _p_inside(short_width: float, sigma_move: float) -> float:
    """P(|move| < short_width) for a zero-mean normal with stdev sigma_move."""
    if sigma_move <= 0:
        return 1.0
    return max(0.0, 2.0 * _norm_cdf(short_width / sigma_move) - 1.0)


class S2ZeroDteVrp:
    """0DTE VRP defined-risk iron condor (short premium when IV is rich)."""

    strategy_id = "S2_0dte_vrp"
    needs_options = True
    needs_rv = True

    def __init__(
        self,
        *,
        k_short: float = 1.0,
        wing_mult: float = 1.0,
        vrp_threshold: float = 0.02,
        min_seconds_to_close: float = 1800.0,
        leg_spread_pct: float = 0.03,
        leg_spread_floor: float = 0.05,
    ) -> None:
        self.k_short = k_short
        self.wing_mult = wing_mult
        self.vrp_threshold = vrp_threshold
        self.min_seconds_to_close = min_seconds_to_close
        self.leg_spread_pct = leg_spread_pct
        self.leg_spread_floor = leg_spread_floor

    def propose(self, fr: FeatureRow, *, config: EngineConfig) -> SignalProposal | None:
        if (
            fr.last_price is None
            or fr.atm_iv is None
            or fr.rv is None
            or fr.vrp is None
            or fr.gex_total is None
        ):
            return None
        # Sell premium only when IV is richly above realized AND net dealer gamma is
        # POSITIVE (vol-suppressing, DESIGN §5 S2 — the regime "gamma is positive").
        # We gate on the GEX sign rather than the discrete regime label so a
        # positive-gamma near-flip still qualifies. No naked short gamma (the wings
        # cap the loss); event days are vetoed by the event-lockout reviewer.
        if fr.gex_total <= 0.0 or fr.vrp < self.vrp_threshold:
            return None

        spot = fr.last_price
        _, close_utc = session_bounds_utc(fr.as_of.date(), config.session)
        secs = (close_utc - fr.as_of).total_seconds()
        if secs < self.min_seconds_to_close:
            return None  # too late to hold a meaningful 0DTE structure
        t = secs / _TRADING_YEAR_SECONDS
        if t <= 0 or fr.atm_iv <= 0:
            return None

        sigma_impl = fr.atm_iv * math.sqrt(t) * spot
        sigma_real = fr.rv * math.sqrt(t) * spot
        if sigma_impl <= 0:
            return None
        # Honesty guard: a non-positive realized-vol estimate (a flat/degenerate RV
        # window) would make P(inside) collapse to a certainty (_p_inside returns 1.0
        # for sigma<=0), inflating win_prob to the 0.99 cap on no real evidence. Refuse
        # to trade on an un-assessable realized distribution rather than guess.
        if sigma_real <= 0:
            return None

        short_width = self.k_short * sigma_impl
        wing_width = self.wing_mult * sigma_impl

        from engine.option_pricer import black_scholes_price  # lazy (SWE)

        r = config.gate.risk_free_rate
        iv = fr.atm_iv
        sc = black_scholes_price(spot, spot + short_width, t, r, iv, "call")
        lc = black_scholes_price(spot, spot + short_width + wing_width, t, r, iv, "call")
        sp = black_scholes_price(spot, spot - short_width, t, r, iv, "put")
        lp = black_scholes_price(spot, spot - short_width - wing_width, t, r, iv, "put")
        credit_ps = (sc - lc) + (sp - lp)              # per share
        max_loss_ps = wing_width - credit_ps           # per share, capped loss
        if credit_ps <= 0 or max_loss_ps <= 0:
            return None  # degenerate / arbitrage-violating pricing

        win_amount = credit_ps * 100.0                 # $ per spread
        loss_amount = max_loss_ps * 100.0

        # Honest win probability for the BINARY settlement = the realized-vol
        # P(close stays inside the short strikes). Using the realized distribution
        # (not the geometry break-even) means the gate is never fed an inflated p:
        # at no edge (realized == implied) the binary OVERSTATES loss vs a real
        # condor, so EV is negative and the gate blocks; S2 only trades when the
        # VRP edge (realized < implied) is large enough to overcome that
        # conservatism AND costs. p_fair/p_impl are kept for transparency only.
        p_impl_inside = _p_inside(short_width, sigma_impl)
        p_real_inside = _p_inside(short_width, sigma_real)
        win_prob = min(0.99, max(0.01, p_real_inside))
        p_fair = max_loss_ps / (credit_ps + max_loss_ps)

        # Explicit 4-leg round-trip cost (per spread) so the gate never understates.
        legs = [(sc, "call"), (lc, "call"), (sp, "put"), (lp, "put")]
        e_slip = x_slip = comm = imp = tot = 0.0
        for premium, _kind in legs:
            leg_spread = max(self.leg_spread_floor, self.leg_spread_pct * premium)
            cb = round_trip_cost(
                side=Side.SHORT, instrument=AssetKind.OPTION, ref_price=premium,
                spread=leg_spread, size=1, adv=None, config=config.cost,
            )
            e_slip += cb.entry_slippage
            x_slip += cb.exit_slippage
            comm += cb.commission
            imp += cb.impact
            tot += cb.total
        cost_override = CostBreakdown(e_slip, x_slip, comm, imp, tot, {"legs": 4})

        return SignalProposal(
            strategy_id=self.strategy_id,
            symbol=fr.symbol,
            ts=fr.as_of,
            side=Side.SHORT,
            instrument=AssetKind.OPTION,
            ref_price=spot,
            target_price=spot,   # unused for a structure (overrides drive economics)
            stop_price=spot,
            win_prob=win_prob,
            spread=0.0,
            win_amount=win_amount,
            loss_amount=loss_amount,
            cost_override=cost_override,
            meta={
                "structured": True,
                "center": spot,
                "short_width": short_width,
                "wing_width": wing_width,
                "credit_ps": credit_ps,
                "max_loss_ps": max_loss_ps,
                "p_fair": p_fair,
                "p_impl_inside": p_impl_inside,
                "p_real_inside": p_real_inside,
                "vrp_edge": p_real_inside - p_impl_inside,
                "atm_iv": iv,
                "rv": fr.rv,
                "gex_total": fr.gex_total,
                "regime": (fr.gamma_regime.value if fr.gamma_regime else None),
            },
        )
