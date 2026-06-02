"""Central configuration for the intraday engine.

All tunable knobs live here as frozen dataclasses so a run is fully described by
one :class:`EngineConfig` value (reproducibility). Values that are *assumptions*
pending operator input (DESIGN.md §11) are flagged in comments and must never be
treated as confirmed live-money parameters.

Units throughout:
- rates / percentages: decimal fractions (0.04 = 4%), unless suffixed ``_pct``
  which is also decimal (0.03 = 3%);
- money: US dollars;
- volatility: annualized decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

# The Phase-1 "gamma spine" universe (DESIGN.md §1).
DEFAULT_SYMBOLS: tuple[str, ...] = ("SPX", "SPY", "QQQ")


@dataclass(frozen=True)
class CostConfig:
    """Transaction-cost model knobs (consumed by :mod:`intraday.costs`).

    These mirror / drive ``engine.transaction_costs``; ``impact_coefficient`` is
    the Almgren-Chriss square-root-impact coefficient. Defaults are deliberately
    conservative — costs dominate intraday and we never want to understate them.
    """

    impact_coefficient: float = 0.10
    use_sqrt_impact: bool = True
    option_commission_per_contract: float = 0.65  # SWE DEFAULT_COMMISSION_PER_CONTRACT
    stock_commission_per_share: float = 0.0       # SWE "stock" trade_type → 0.0
    # Minimum modelled spread as a fraction of price when no quote is available.
    fallback_spread_pct: float = 0.0005           # 5 bps of price (liquid index ETFs)


@dataclass(frozen=True)
class RiskConfig:
    """Sizing, caps, and the daily kill-switch (DESIGN.md §7).

    ``paper_nav`` is an ASSUMPTION for sizing math only (DESIGN.md §11 open
    question #3) — it is NOT a confirmed account size and must never be used as a
    live-money parameter. PDT awareness is logged, not enforced as a hard block.
    """

    paper_nav: float = 100_000.0          # ASSUMPTION (DESIGN §11) — sizing only
    kelly_fraction: float = 0.5           # fractional (half-) Kelly
    max_risk_per_trade_pct: float = 0.01  # cap $ risked per trade at 1% of NAV
    max_position_notional_pct: float = 0.20
    max_concurrent_positions: int = 3
    daily_loss_limit_pct: float = 0.03    # hard kill-switch (3% of NAV)
    min_size: int = 1
    max_size: int = 1_000


@dataclass(frozen=True)
class SessionConfig:
    """US equity Regular Trading Hours, in exchange-local time (ET)."""

    tz: str = "America/New_York"
    rth_open: time = time(9, 30)
    rth_close: time = time(16, 0)
    # Hard time-stop: flatten this many minutes before the close (no overnight).
    flatten_before_close_min: int = 5


@dataclass(frozen=True)
class GateConfig:
    """Expectancy-gate authority knobs (DESIGN.md §6)."""

    # Net-of-cost expected $ PnL per trade must STRICTLY exceed this to be
    # tradeable. 0.0 ⇒ require positive net expectancy. Negative / non-finite EV
    # is always blocked regardless of this value.
    ev_threshold: float = 0.0
    risk_free_rate: float = 0.04  # annual decimal, for metrics/pricing


@dataclass(frozen=True)
class DataConfig:
    """Data-layer knobs: arrival latency (drives PIT availability) and the RNG
    seed for the deterministic synthetic provider."""

    bar_latency_ms: int = 250
    chain_latency_ms: int = 1_000
    tape_latency_ms: int = 250
    rng_seed: int = 7
    # Synthetic price-process shape (see intraday.data.synthetic). Defaults make
    # the path random-walk DOMINANT (realistic for liquid index ETFs). Tests dial
    # these up to create a strongly mean-reverting world to prove the engine
    # captures edge when present and not when absent.
    fv_weight: float = 0.85    # fair-value random-walk innovation (× per-sec sigma)
    dev_weight: float = 0.50   # mean-reverting deviation innovation
    ou_phi: float = 0.998      # per-second AR(1) persistence of the deviation


@dataclass(frozen=True)
class EngineConfig:
    """Top-level config bundle. ``EngineConfig.default()`` is the canonical run."""

    cost: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    data: DataConfig = field(default_factory=DataConfig)
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS

    @classmethod
    def default(cls) -> "EngineConfig":
        return cls()
