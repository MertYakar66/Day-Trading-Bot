"""Core data contracts shared across the intraday engine.

This module is the integration backbone: the enums, the point-in-time (PIT) data
containers returned by data providers, and the signal/decision/trade dataclasses
that flow from strategies through the authority gate into the backtest ledger.

POINT-IN-TIME DISCIPLINE (the single most important invariant — DESIGN.md §2.3):
every time-stamped artifact carries an ``available_ts`` (or derives one from a
close timestamp + arrival latency) marking when it *first became usable* from a
live feed. A decision made at time ``T`` may consume a row only if
``available_ts <= T``. The PIT containers below expose ``*_available_at(T)``
slicers that enforce this so look-ahead cannot leak in by accident; the
no-look-ahead property tests assert that appending future rows never changes a
PIT slice taken at ``T``.

UNITS: prices in US dollars (per share / per index point); volatility annualized
decimal; sizes are shares (stock) or contracts (options); a contract is 100
shares (the SWE-wide multiplier).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum

import pandas as pd

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Verdict(IntEnum):
    """Decision verdict. Ordered so a *lower* value is *more restrictive*; this
    makes "downgrade-only" reviewers a simple ``min()`` (DESIGN.md §6).
    """

    BLOCKED = 0
    SKIP = 1
    REVIEW = 2
    PROCEED = 3

    def downgraded_to(self, other: "Verdict") -> "Verdict":
        """Return the more-restrictive of the two — a reviewer can never upgrade."""
        return Verdict(min(int(self), int(other)))


class Side(Enum):
    """Trade direction for a position/signal."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


class AssetKind(Enum):
    """Instrument kind — drives the contract multiplier and the cost model."""

    INDEX = "index"   # e.g. SPX (cash, not directly traded on the paper path)
    STOCK = "stock"   # e.g. SPY / QQQ shares (S3 trades these)
    OPTION = "option"

    @property
    def multiplier(self) -> int:
        """Shares represented by one unit of size."""
        return 100 if self is AssetKind.OPTION else 1


class DataSource(Enum):
    """Provenance of a data frame. SYNTHETIC must never be mistaken for real.

    Real-data sources (the corrected data-tier reality — see PROGRESS.md):
    - ``IBKR``       — underlying intraday bars + live snapshots (reads only):
      SPY/QQQ stock, SPX/VIX index. The real underlying feed.
    - ``THETA``      — OPTIONS ONLY at the operator's STANDARD tier (intraday tape
      + IV + greeks). Theta stock is FREE/EOD-only and Theta index is unavailable,
      so Theta NEVER supplies the underlying here.
    - ``PARITY``     — underlying reconstructed from synchronized ATM call/put
      option quotes via put-call parity (deep history where IBKR is shallow).
    - ``FREE_DAILY`` — free end-of-day underlying (yfinance/Stooq) for daily
      regime/tail context only (no intraday).
    - ``FUSED``      — a composite provider resolving underlying across the above
      and options from Theta (per-frame provenance is preserved on each frame).
    """

    SYNTHETIC = "synthetic"
    THETA = "theta"
    IBKR = "ibkr"
    PARITY = "parity"
    FREE_DAILY = "free_daily"
    FUSED = "fused"

    @property
    def is_synthetic(self) -> bool:
        return self is DataSource.SYNTHETIC

    @property
    def is_real(self) -> bool:
        """True for sources backed by real market data (never SYNTHETIC)."""
        return self in (
            DataSource.IBKR,
            DataSource.THETA,
            DataSource.PARITY,
            DataSource.FREE_DAILY,
            DataSource.FUSED,
        )


class GammaRegime(Enum):
    """Dealer-gamma regime — the master switch (DESIGN.md §4/§5). Values match
    ``engine.dealer_positioning`` regime strings so they round-trip exactly.
    """

    LONG_GAMMA = "long_gamma_dampening"     # +GEX → dealers fade → mean-reverting
    SHORT_GAMMA = "short_gamma_amplifying"  # -GEX → dealers chase → trending
    NEAR_FLIP = "near_flip"                 # close to the gamma-flip → unstable
    NEUTRAL = "neutral"


# Canonical column schemas (documented; validated by the store and tests).
BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
TAPE_COLUMNS: tuple[str, ...] = (
    "ts", "available_ts", "expiration", "strike", "right",
    "price", "size", "nbbo_bid", "nbbo_ask", "side_inferred",
)
CHAIN_COLUMNS: tuple[str, ...] = (
    "snapshot_ts", "available_ts", "expiration", "strike", "option_type",
    "open_interest", "implied_vol", "spot",
)


# --------------------------------------------------------------------------- #
# PIT data containers (provider return types)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BarSeries:
    """An OHLCV bar series with arrival-latency-aware PIT access.

    ``frame`` is indexed by the bar **close** timestamp (tz-aware UTC) and holds
    :data:`BAR_COLUMNS`. A bar closing at ``t`` is usable only at ``t + latency``.
    """

    symbol: str
    interval: str  # e.g. "1s", "1m"
    frame: pd.DataFrame
    source: DataSource
    latency: pd.Timedelta = field(default_factory=lambda: pd.Timedelta(0))

    def available_at(self, as_of: pd.Timestamp) -> pd.DataFrame:
        """Bars whose close + latency is at or before ``as_of`` (no look-ahead)."""
        cutoff = pd.Timestamp(as_of) - self.latency
        return self.frame.loc[self.frame.index <= cutoff]

    @property
    def is_synthetic(self) -> bool:
        return self.source.is_synthetic


@dataclass(frozen=True)
class OptionChainSeries:
    """A time series of option-chain snapshots with PIT access.

    ``frame`` has one row per ``(snapshot_ts, expiration, strike, option_type)``
    with at least :data:`CHAIN_COLUMNS`. ``implied_vol`` is annualized decimal;
    ``spot`` is the underlying price at the snapshot. ``available_ts`` is the
    snapshot time + latency.
    """

    symbol: str
    frame: pd.DataFrame
    source: DataSource

    def latest_available(self, as_of: pd.Timestamp) -> pd.DataFrame:
        """The most recent single snapshot whose ``available_ts <= as_of``.

        Returns an empty frame (with the right columns) if nothing is available.
        """
        as_of = pd.Timestamp(as_of)
        avail = self.frame.loc[self.frame["available_ts"] <= as_of]
        if avail.empty:
            return avail
        last = avail["snapshot_ts"].max()
        return avail.loc[avail["snapshot_ts"] == last]

    @property
    def is_synthetic(self) -> bool:
        return self.source.is_synthetic


@dataclass(frozen=True)
class OptionTape:
    """Trade-by-trade option tape with NBBO + buy/sell classification.

    ``frame`` holds :data:`TAPE_COLUMNS`; ``side_inferred`` ∈ {"buy","sell","mid"}
    matches ``scripts/pull_theta_option_tape.py`` semantics (buy = customer
    buy-initiated = dealer sells). ``available_ts`` is the print time + latency.
    """

    symbol: str
    frame: pd.DataFrame
    source: DataSource

    def window_available_at(
        self, as_of: pd.Timestamp, lookback: pd.Timedelta
    ) -> pd.DataFrame:
        """Prints available by ``as_of`` whose trade time is within ``lookback``."""
        as_of = pd.Timestamp(as_of)
        avail = self.frame.loc[self.frame["available_ts"] <= as_of]
        return avail.loc[avail["ts"] > as_of - lookback]

    @property
    def is_synthetic(self) -> bool:
        return self.source.is_synthetic


# --------------------------------------------------------------------------- #
# Signal / decision / cost / trade
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SignalProposal:
    """A strategy's per-unit trade idea (pre-sizing, pre-gate).

    Two flavours, both gated identically by net-of-cost expectancy:

    1. **Directional** (S1/S3): price geometry — ``ref_price``/``target_price``/
       ``stop_price``. Per-unit gross dollars are ``reward/risk_per_unit ×
       multiplier`` (e.g. shares × $/share).
    2. **Defined-risk structure** (S2): the economics are given DIRECTLY as
       ``win_amount`` (credit kept, $/unit) and ``loss_amount`` (max loss, $/unit,
       a positive magnitude). For these, ``ref_price`` is the underlying spot and
       ``cost_override`` carries the (multi-leg) round-trip cost so the gate does
       not understate it. Settlement params live in ``meta["settlement"]``.

    ``win_prob`` (``p``) is supplied either way. The proposal is size-agnostic so
    the risk sizer chooses size first; the gate then evaluates EV at that size.
    """

    strategy_id: str
    symbol: str
    ts: pd.Timestamp        # decision time (UTC); built only from data <= ts
    side: Side
    instrument: AssetKind
    ref_price: float        # reference price observed at ts (NOT the fill price)
    target_price: float
    stop_price: float
    win_prob: float         # p ∈ [0, 1]
    spread: float           # observed bid-ask spread at ts (per share, $)
    adv: float | None = None  # avg daily volume (shares/contracts) for impact
    # Defined-risk overrides (None ⇒ directional, derive from price geometry):
    win_amount: float | None = None   # gross $ gain per unit if it wins
    loss_amount: float | None = None  # gross $ loss per unit if it loses (>= 0)
    cost_override: "CostBreakdown | None" = None  # explicit round-trip cost ($/unit-set)
    meta: Mapping = field(default_factory=dict)

    @property
    def reward_per_unit(self) -> float:
        """Gross $ gain per share if the target is hit (directional geometry)."""
        return abs(self.target_price - self.ref_price)

    @property
    def risk_per_unit(self) -> float:
        """Gross $ loss per share if the stop is hit (directional geometry)."""
        return abs(self.ref_price - self.stop_price)

    @property
    def is_structured(self) -> bool:
        """True for a defined-risk structure (credit/max-loss given directly)."""
        return self.win_amount is not None and self.loss_amount is not None

    @property
    def reward_dollars(self) -> float:
        """Gross $ gain PER UNIT (per contract/spread/share-lot) if it wins."""
        if self.win_amount is not None:
            return self.win_amount
        return self.reward_per_unit * self.instrument.multiplier

    @property
    def risk_dollars(self) -> float:
        """Gross $ loss PER UNIT if it loses (positive magnitude)."""
        if self.loss_amount is not None:
            return self.loss_amount
        return self.risk_per_unit * self.instrument.multiplier

    @property
    def notional_per_unit(self) -> float:
        """Capital basis per unit for the position-notional cap: the underlying
        notional for a directional trade, or the defined max loss for a structure."""
        if self.loss_amount is not None:
            return self.loss_amount
        return self.ref_price * self.instrument.multiplier


@dataclass(frozen=True)
class CostBreakdown:
    """Round-trip transaction cost in dollars at the contemplated size.

    Components are additive: ``total = entry_slippage + exit_slippage +
    commission`` where each slippage term already includes spread + square-root
    market impact and is expressed in dollars at the full size. ``impact`` is the
    size-dependent portion broken out for transparency (already inside the
    slippage terms — do not add it again).
    """

    entry_slippage: float
    exit_slippage: float
    commission: float
    impact: float
    total: float
    detail: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    """The authority's verdict on a sized proposal (DESIGN.md §6)."""

    proposal: SignalProposal
    size: int
    verdict: Verdict
    ev_gross: float        # p·win − (1−p)·loss   ($, at size)
    ev_net: float          # ev_gross − cost.total ($, at size)
    cost: CostBreakdown
    threshold: float
    reason: str
    trail: tuple[str, ...] = ()  # downgrade reasons appended by reviewers

    @property
    def tradeable(self) -> bool:
        return self.verdict is Verdict.PROCEED

    def with_downgrade(self, verdict: Verdict, reason: str) -> "GateResult":
        """Return a copy downgraded to ``verdict`` (never upgraded), recording
        ``reason`` in the trail. A no-op if ``verdict`` is not more restrictive.
        """
        new_verdict = self.verdict.downgraded_to(verdict)
        if new_verdict == self.verdict:
            return self
        from dataclasses import replace

        return replace(
            self, verdict=new_verdict, trail=self.trail + (reason,)
        )


@dataclass(frozen=True)
class Fill:
    """A single simulated paper fill (open or close leg)."""

    ts: pd.Timestamp
    symbol: str
    side: Side
    size: int
    price: float
    kind: str            # "OPEN" | "CLOSE"
    cost: float          # $ cost attributed to this leg
    reason: str = ""


@dataclass
class Position:
    """An open paper position tracked by the backtest/ledger."""

    symbol: str
    strategy_id: str
    side: Side
    size: int
    instrument: AssetKind
    decision_ts: pd.Timestamp
    entry_ts: pd.Timestamp
    entry_price: float        # actual (conservative) fill price
    target_price: float
    stop_price: float
    entry_cost: float         # $ entry-leg cost
    flatten_at: pd.Timestamp  # hard intraday time-stop
    meta: Mapping = field(default_factory=dict)

    @property
    def multiplier(self) -> int:
        return self.instrument.multiplier

    def unrealized_pnl(self, mark: float) -> float:
        """Mark-to-market gross PnL (excludes the not-yet-paid exit cost)."""
        return self.side.sign * (mark - self.entry_price) * self.size * self.multiplier


@dataclass(frozen=True)
class Trade:
    """A closed round-trip — the unit of performance accounting."""

    symbol: str
    strategy_id: str
    side: Side
    size: int
    instrument: AssetKind
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str

    @property
    def hold_seconds(self) -> float:
        return (self.exit_ts - self.entry_ts).total_seconds()

    def to_metrics_row(self) -> dict:
        """Row shape consumed by ``engine.performance_metrics`` (DESIGN.md §8).

        Convention there: ``net_pnl = realized_pnl − transaction_costs``.
        ``hold_days`` is the intraday hold expressed in days for the (daily)
        metrics module; it is informational for intraday trades.
        """
        return {
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "net_pnl": self.net_pnl,
            "realized_pnl": self.gross_pnl,
            "transaction_costs": self.costs,
            "hold_days": self.hold_seconds / 86_400.0,
            "exit_reason": self.exit_reason,
        }
