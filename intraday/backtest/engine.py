"""Event-driven intraday backtester (DESIGN.md §8; TASKS.md T0.4).

Design note (deviation from TASKS.md wording, documented in PROGRESS.md): SWE's
``backtests.simulator.WheelBacktester`` is a *daily, wheel-specific, placeholder*
backtester coupled to ``WheelTracker``. Forcing intraday multi-instrument event
replay onto it would be wrong, so this is a purpose-built engine that REUSES SWE's
quant pieces (costs, pricer, metrics, dealer, RV) and SWE's discipline patterns
(profit-target / stop / mark-to-market) while honoring "never modify SWE".

No-look-ahead guarantees:
- A decision at bar ``i`` (close ``t_i``, actionable at ``t_i + latency``) uses
  only the *causal* feature value at position ``i`` — by construction it cannot see
  bar ``i+1``. (A test asserts ``FeaturePipeline.row(as_of_i)`` equals this
  positional read.)
- Fills occur at bar ``i+1``'s open with adverse slippage (conservative fills).
- Stops/targets are detected on the just-closed bar and executed one bar later.
- Positions flatten before the close — no overnight risk.

Risk controls: per-symbol single position, a global concurrency cap, and a daily
loss kill-switch that latches for the rest of the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..config import EngineConfig
from ..contracts import (
    AssetKind,
    DataSource,
    Fill,
    Position,
    Side,
    Trade,
    Verdict,
)
from ..data.provider import DataProvider, asset_kind_for
from ..data.quality import assert_no_feed_gap
from ..features.base import FeatureRow
from ..features.pipeline import FeaturePipeline
from ..logging_config import get_logger
from ..risk.sizing import kelly_size
from ..signals.base import Strategy
from ..timeutils import flatten_time_utc
from ..authority.gate import ExpectancyGate
from ..authority.reviewers import ReviewContext, ReviewerPipeline, default_reviewers
from .fills import conservative_fill

logger = get_logger(__name__)


@dataclass
class BacktestResult:
    config: EngineConfig
    symbols: list[str]
    interval: str
    data_source: DataSource
    initial_capital: float
    final_equity: float
    trades: list[Trade]
    fills: list[Fill]
    equity_curve: list[dict]      # one row per trading day: {date, portfolio_value}
    signals: list[dict]           # every gated proposal (audit)
    n_days: int

    def closed_trade_rows(self) -> list[dict]:
        return [t.to_metrics_row() for t in self.trades]


class IntradayBacktester:
    """Replays stored/synthetic bars in timestamp order, gating every signal."""

    def __init__(
        self,
        config: EngineConfig,
        provider: DataProvider,
        strategies: list[Strategy],
        *,
        reviewers: ReviewerPipeline | None = None,
        record_signals: bool = True,
    ) -> None:
        self.config = config
        self.provider = provider
        self.strategies = strategies
        self.gate = ExpectancyGate(config)
        self.reviewers = reviewers or default_reviewers(config)
        self.pipeline = FeaturePipeline(config)
        self.record_signals = record_signals

    # ------------------------------------------------------------------ #
    def run(
        self,
        symbols: list[str],
        start: date,
        end: date,
        interval: str = "1m",
    ) -> BacktestResult:
        cfg = self.config
        nav0 = cfg.risk.paper_nav
        equity = nav0
        trades: list[Trade] = []
        fills: list[Fill] = []
        equity_curve: list[dict] = []
        signals: list[dict] = []

        days = self.provider.trading_days(start, end)
        for day in days:
            equity = self._run_day(
                day, symbols, interval, nav0, equity, trades, fills, signals
            )
            equity_curve.append(
                {"date": pd.Timestamp(day).isoformat(), "portfolio_value": equity}
            )

        return BacktestResult(
            config=cfg,
            symbols=list(symbols),
            interval=interval,
            data_source=self.provider.source,
            initial_capital=nav0,
            final_equity=equity,
            trades=trades,
            fills=fills,
            equity_curve=equity_curve,
            signals=signals,
            n_days=len(days),
        )

    # ------------------------------------------------------------------ #
    def _run_day(
        self,
        day: date,
        symbols: list[str],
        interval: str,
        nav0: float,
        equity: float,
        trades: list[Trade],
        fills: list[Fill],
        signals: list[dict],
    ) -> float:
        cfg = self.config
        # Load + precompute per symbol.
        loaded: dict[str, dict] = {}
        for sym in symbols:
            bars = self.provider.get_bars(sym, day, interval)
            # DESIGN §2.4 freshness: halt on a gapped feed rather than guess.
            assert_no_feed_gap(bars.frame.index, interval)
            sf = self.pipeline.precompute(bars, day)
            frame = bars.frame
            or_known_i = self._orb_known_index(frame.index, sf)
            loaded[sym] = {
                "bars": bars,
                "frame": frame,
                "open": frame["open"].to_numpy(),
                "high": frame["high"].to_numpy(),
                "low": frame["low"].to_numpy(),
                "close": frame["close"].to_numpy(),
                "vwap": sf.vwap["vwap"].to_numpy(),
                "sigma": sf.vwap["vwap_sigma"].to_numpy(),
                "dev": sf.vwap["vwap_dev_sigma"].to_numpy(),
                "sf": sf,
                "or_known_i": or_known_i,
            }

        ref = loaded[symbols[0]]
        index = ref["frame"].index
        n = len(index)
        # All symbols share the same RTH bar grid; guard against a mismatch so an
        # accidental misalignment surfaces loudly rather than silently using i on
        # a differently-indexed frame.
        for sym in symbols:
            if len(loaded[sym]["frame"].index) != n:
                raise ValueError(
                    f"bar index length mismatch for {sym}: "
                    f"{len(loaded[sym]['frame'].index)} != {n}"
                )
        bar_latency = loaded[symbols[0]]["bars"].latency
        flatten_at = flatten_time_utc(day, cfg.session)

        positions: dict[str, Position] = {}
        day_realized = 0.0
        session_killed = False
        daily_budget = cfg.risk.daily_loss_limit_pct * nav0

        for i in range(n):
            ts = index[i]
            as_of = ts + bar_latency

            # 1) Manage exits first (free up concurrency before new entries).
            for sym in list(positions.keys()):
                pos = positions[sym]
                d = loaded[sym]
                reason = self._exit_reason(pos, d, i, ts, flatten_at)
                if reason is None:
                    continue
                fill_price, exit_cost, fill_ts = self._exit_fill(pos, d, i, index, n)
                gross = pos.side.sign * (fill_price - pos.entry_price) * pos.size * pos.multiplier
                costs = pos.entry_cost + exit_cost
                net = gross - costs
                day_realized += net
                equity += net
                trades.append(
                    Trade(
                        symbol=sym, strategy_id=pos.strategy_id, side=pos.side,
                        size=pos.size, instrument=pos.instrument,
                        entry_ts=pos.entry_ts, exit_ts=fill_ts,
                        entry_price=pos.entry_price, exit_price=fill_price,
                        gross_pnl=gross, costs=costs, net_pnl=net, exit_reason=reason,
                    )
                )
                fills.append(
                    Fill(fill_ts, sym, _close_side(pos.side), pos.size, fill_price,
                         "CLOSE", exit_cost, reason)
                )
                del positions[sym]
                if daily_budget > 0 and day_realized <= -daily_budget:
                    session_killed = True

            # 2) Entries — only if we can still fill next bar and before flatten.
            if i + 1 >= n or ts >= flatten_at:
                continue
            for sym in symbols:
                if sym in positions:
                    continue
                if len(positions) >= cfg.risk.max_concurrent_positions:
                    break
                d = loaded[sym]
                fr = self._feature_row(sym, as_of, d, i)
                opened = self._try_enter(
                    sym, fr, d, i, index, nav0, day_realized, session_killed,
                    positions, fills, signals, flatten_at,
                )
                if opened:
                    continue

        # Safety: flatten anything still open at the last close (should be none,
        # since the time-stop fires at flatten_at). Charge the exit cost too.
        for sym in list(positions.keys()):
            pos = positions[sym]
            d = loaded[sym]
            fill_price = float(d["close"][n - 1])
            _, exit_cost = conservative_fill(
                side=pos.side, instrument=pos.instrument, next_open=fill_price,
                spread=cfg.cost.fallback_spread_pct * fill_price, size=pos.size,
                adv=pos.meta.get("adv"), config=cfg.cost, is_entry=False,
            )
            gross = pos.side.sign * (fill_price - pos.entry_price) * pos.size * pos.multiplier
            costs = pos.entry_cost + exit_cost
            net = gross - costs
            day_realized += net
            equity += net
            trades.append(
                Trade(symbol=sym, strategy_id=pos.strategy_id, side=pos.side,
                      size=pos.size, instrument=pos.instrument, entry_ts=pos.entry_ts,
                      exit_ts=index[n - 1], entry_price=pos.entry_price,
                      exit_price=fill_price, gross_pnl=gross, costs=costs,
                      net_pnl=net, exit_reason="eod_safety")
            )
            del positions[sym]

        return equity

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _orb_known_index(index: pd.DatetimeIndex, sf) -> int:
        """First bar position at which the opening range is known."""
        if sf.opening_range is None:
            return 10**9
        we = sf.opening_range.window_end
        pos = index.searchsorted(we, side="left")
        return int(pos)

    def _feature_row(self, sym: str, as_of: pd.Timestamp, d: dict, i: int) -> FeatureRow:
        dev = d["dev"][i]
        sigma = d["sigma"][i]
        orb_known = i >= d["or_known_i"]
        sf = d["sf"]
        return FeatureRow(
            symbol=sym,
            as_of=as_of,
            last_price=float(d["close"][i]),
            vwap=float(d["vwap"][i]),
            vwap_sigma=None if not np.isfinite(sigma) else float(sigma),
            vwap_dev_sigma=None if not np.isfinite(dev) else float(dev),
            orb_high=(sf.opening_range.high if orb_known else None),
            orb_low=(sf.opening_range.low if orb_known else None),
            orb_volume=(sf.opening_range.volume if orb_known else None),
            meta={},
        )

    def _try_enter(
        self, sym, fr, d, i, index, nav0, day_realized, session_killed,
        positions, fills, signals, flatten_at,
    ) -> bool:
        cfg = self.config
        for strat in self.strategies:
            prop = strat.propose(fr, config=cfg)
            if prop is None:
                continue
            sizing = kelly_size(prop, cfg.risk)
            if sizing.size <= 0:
                continue
            gres = self.gate.evaluate(prop, sizing.size)
            ctx = ReviewContext(
                as_of=fr.as_of, symbol=sym, feature_row=fr,
                daily_realized_pnl=day_realized, nav=nav0,
                session_killed=session_killed,
            )
            gres = self.reviewers.apply(gres, ctx)
            if self.record_signals:
                signals.append(
                    {
                        "as_of": pd.Timestamp(fr.as_of).isoformat(),
                        "symbol": sym, "strategy_id": prop.strategy_id,
                        "side": prop.side.value, "size": sizing.size,
                        "verdict": gres.verdict.name, "reason": gres.reason,
                        "ev_gross": gres.ev_gross, "ev_net": gres.ev_net,
                        "cost": gres.cost.total, "trail": ";".join(gres.trail),
                    }
                )
            if not gres.tradeable:
                continue
            # Open at next bar's open with adverse slippage.
            nxt_open = float(d["open"][i + 1])
            fill_price, entry_cost = conservative_fill(
                side=prop.side, instrument=prop.instrument, next_open=nxt_open,
                spread=prop.spread, size=sizing.size, adv=prop.adv,
                config=cfg.cost, is_entry=True,
            )
            pos = Position(
                symbol=sym, strategy_id=prop.strategy_id, side=prop.side,
                size=sizing.size, instrument=prop.instrument,
                decision_ts=fr.as_of, entry_ts=index[i + 1], entry_price=fill_price,
                target_price=prop.target_price, stop_price=prop.stop_price,
                entry_cost=entry_cost, flatten_at=flatten_at,
                meta={"spread": prop.spread, "adv": prop.adv},
            )
            positions[sym] = pos
            fills.append(Fill(index[i + 1], sym, prop.side, sizing.size,
                              fill_price, "OPEN", entry_cost, prop.strategy_id))
            return True
        return False

    @staticmethod
    def _exit_reason(pos: Position, d: dict, i: int, ts: pd.Timestamp, flatten_at) -> str | None:
        hi = float(d["high"][i])
        lo = float(d["low"][i])
        if pos.side is Side.LONG:
            if lo <= pos.stop_price:
                return "stop"
            if hi >= pos.target_price:
                return "target"
        else:  # SHORT
            if hi >= pos.stop_price:
                return "stop"
            if lo <= pos.target_price:
                return "target"
        if ts >= flatten_at:
            return "time_stop"
        return None

    def _exit_fill(self, pos: Position, d: dict, i: int, index, n: int):
        """Conservative exit: fill at next bar open (or this close if last bar)."""
        if i + 1 < n:
            ref_open = float(d["open"][i + 1])
            fill_ts = index[i + 1]
        else:
            ref_open = float(d["close"][i])
            fill_ts = index[i]
        spread = self.config.cost.fallback_spread_pct * ref_open
        fill_price, exit_cost = conservative_fill(
            side=pos.side, instrument=pos.instrument, next_open=ref_open,
            spread=spread, size=pos.size, adv=pos.meta.get("adv"),
            config=self.config.cost, is_entry=False,
        )
        return fill_price, exit_cost, fill_ts


def _close_side(side: Side) -> Side:
    return Side.SHORT if side is Side.LONG else Side.LONG
