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

    def __post_init__(self) -> None:
        # Costs may never be negative — a negative cost would silently inflate PnL.
        for name in (
            "impact_coefficient", "option_commission_per_contract",
            "stock_commission_per_share", "fallback_spread_pct",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"CostConfig.{name} must be >= 0, got {getattr(self, name)!r}")


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

    def __post_init__(self) -> None:
        # Reject nonsensical risk parameters early (e.g. a CLI ``--nav -1000``)
        # rather than crashing deep in sizing/gate or producing silent nonsense.
        if self.paper_nav <= 0:
            raise ValueError(f"RiskConfig.paper_nav must be > 0, got {self.paper_nav!r}")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError(
                f"RiskConfig.kelly_fraction must be in (0, 1], got {self.kelly_fraction!r}"
            )
        for name in ("max_risk_per_trade_pct", "max_position_notional_pct"):
            v = getattr(self, name)
            if not 0 < v <= 1:
                raise ValueError(f"RiskConfig.{name} must be in (0, 1], got {v!r}")
        if not 0 <= self.daily_loss_limit_pct <= 1:
            raise ValueError(
                f"RiskConfig.daily_loss_limit_pct must be in [0, 1], "
                f"got {self.daily_loss_limit_pct!r}"
            )
        if self.max_concurrent_positions < 1:
            raise ValueError(
                f"RiskConfig.max_concurrent_positions must be >= 1, "
                f"got {self.max_concurrent_positions!r}"
            )
        if self.min_size < 1:
            raise ValueError(f"RiskConfig.min_size must be >= 1, got {self.min_size!r}")
        if self.max_size < self.min_size:
            raise ValueError(
                f"RiskConfig.max_size ({self.max_size!r}) must be >= "
                f"min_size ({self.min_size!r})"
            )


@dataclass(frozen=True)
class SessionConfig:
    """US equity Regular Trading Hours, in exchange-local time (ET)."""

    tz: str = "America/New_York"
    rth_open: time = time(9, 30)
    rth_close: time = time(16, 0)
    # Hard time-stop: flatten this many minutes before the close (no overnight).
    flatten_before_close_min: int = 5

    def __post_init__(self) -> None:
        if self.rth_open >= self.rth_close:
            raise ValueError(
                f"SessionConfig.rth_open ({self.rth_open}) must be before "
                f"rth_close ({self.rth_close})"
            )
        if self.flatten_before_close_min < 0:
            raise ValueError(
                f"SessionConfig.flatten_before_close_min must be >= 0, "
                f"got {self.flatten_before_close_min!r}"
            )


@dataclass(frozen=True)
class GateConfig:
    """Expectancy-gate authority knobs (DESIGN.md §6)."""

    # Net-of-cost expected $ PnL per trade must STRICTLY exceed this to be
    # tradeable. 0.0 ⇒ require positive net expectancy. Negative / non-finite EV
    # is always blocked regardless of this value.
    ev_threshold: float = 0.0
    risk_free_rate: float = 0.04  # annual decimal, for metrics/pricing

    def __post_init__(self) -> None:
        # Negative EV is always blocked by the gate before the threshold is read, so
        # a negative threshold is meaningless; require >= 0.
        if self.ev_threshold < 0:
            raise ValueError(f"GateConfig.ev_threshold must be >= 0, got {self.ev_threshold!r}")
        if not 0 <= self.risk_free_rate <= 1:
            raise ValueError(
                f"GateConfig.risk_free_rate must be in [0, 1] (annual decimal), "
                f"got {self.risk_free_rate!r}"
            )


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
    # Synthetic option-chain dealer-gamma skew amplitude: how strongly call vs put
    # OI tilts intraday so the GEX regime actually swings long/short gamma (vs a
    # perpetual near-flip). 0 ⇒ symmetric chain (always near-flip).
    chain_gamma_skew: float = 0.45
    # How often the backtest recomputes the (expensive) dealer GEX/flip/walls
    # structure. The regime is slow-moving, so 30 min is ample and keeps runs fast.
    gex_recompute_min: int = 30


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
    def default(cls) -> EngineConfig:
        return cls()
