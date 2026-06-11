"""Single-shot, read-only, point-in-time decision poll (PAPER ONLY).

Reuses the exact decision path of the backtester for ONE timestamp: PIT features →
fractional-Kelly sizing → net-of-cost expectancy gate → downgrade-only reviewers.
No broker, no orders — the output is an advisory list of gated paper decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ..authority.gate import ExpectancyGate
from ..authority.reviewers import ReviewContext, ReviewerPipeline, default_reviewers
from ..config import EngineConfig
from ..data.provider import DataProvider
from ..features.pipeline import FeaturePipeline
from ..risk.sizing import kelly_size
from ..signals.base import Strategy


@dataclass(frozen=True)
class LiveDecision:
    """One strategy's gated paper decision at a poll time (advisory; no order)."""

    symbol: str
    strategy_id: str
    as_of: pd.Timestamp
    verdict: str            # PROCEED / REVIEW / SKIP / BLOCKED
    side: str
    size: int
    ev_gross: float
    ev_net: float
    cost: float
    reason: str
    tradeable: bool         # PROCEED and a positive size
    trail: tuple[str, ...] = field(default_factory=tuple)

    def to_signal_row(self) -> dict:
        """The exact signal-record shape the backtest writes (record parity,
        DESIGN §8) — so a persisted poll and a backtest are directly diffable."""
        return {
            "as_of": pd.Timestamp(self.as_of).isoformat(),
            "symbol": self.symbol, "strategy_id": self.strategy_id,
            "side": self.side, "size": self.size,
            "verdict": self.verdict, "reason": self.reason,
            "ev_gross": self.ev_gross, "ev_net": self.ev_net,
            "cost": self.cost, "trail": ";".join(self.trail),
        }


class LivePoller:
    """Evaluate strategies at a single point in time from a read-only provider."""

    def __init__(
        self,
        config: EngineConfig,
        provider: DataProvider,
        strategies: list[Strategy],
        *,
        reviewers: ReviewerPipeline | None = None,
        interval: str = "5m",
    ) -> None:
        self.config = config
        self.provider = provider
        self.strategies = strategies
        self.gate = ExpectancyGate(config)
        self.reviewers = reviewers or default_reviewers(config)
        self.pipeline = FeaturePipeline(config)
        self.interval = interval

    def poll(self, symbol: str, day: date, as_of: pd.Timestamp) -> list[LiveDecision]:
        """Gated paper decisions for ``symbol`` at ``as_of`` (no orders placed)."""
        bars = self.provider.get_bars(symbol, day, self.interval)
        # Underlying-only feature row (chain/tape omitted; option strategies need a
        # store-backed chain/tape — out of scope for a read-only underlying poll).
        fr = self.pipeline.row(symbol, day, pd.Timestamp(as_of), bars=bars, interval=self.interval)

        decisions: list[LiveDecision] = []
        for strat in self.strategies:
            prop = strat.propose(fr, config=self.config)
            if prop is None:
                continue
            sizing = kelly_size(prop, self.config.risk)
            gres = self.gate.evaluate(prop, max(sizing.size, 1))  # EV at >=1 for reporting
            ctx = ReviewContext(
                as_of=fr.as_of, symbol=symbol, feature_row=fr,
                daily_realized_pnl=0.0, nav=self.config.risk.paper_nav, session_killed=False,
            )
            gres = self.reviewers.apply(gres, ctx)
            decisions.append(LiveDecision(
                symbol=symbol,
                strategy_id=prop.strategy_id,
                as_of=fr.as_of,
                verdict=gres.verdict.name,
                side=prop.side.value,
                size=sizing.size,
                ev_gross=gres.ev_gross,
                ev_net=gres.ev_net,
                cost=gres.cost.total,
                reason=gres.reason,
                tradeable=bool(gres.tradeable and sizing.size > 0),
                trail=tuple(gres.trail),
            ))
        return decisions
