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

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..authority.gate import ExpectancyGate
from ..authority.reviewers import ReviewContext, ReviewerPipeline, default_reviewers
from ..config import EngineConfig
from ..contracts import (
    DataSource,
    Fill,
    GammaRegime,
    Position,
    Side,
    Trade,
)
from ..data.provider import DataProvider
from ..data.quality import assert_finite_bars, assert_no_feed_gap
from ..features.base import FeatureRow
from ..features.gex import gamma_structure_at
from ..features.ofi import ofi_at
from ..features.pipeline import FeaturePipeline
from ..features.realized_vol import intraday_rv
from ..features.vrp import atm_iv_at
from ..logging_config import get_logger
from ..risk.sizing import kelly_size
from ..signals.base import Strategy
from ..timeutils import flatten_time_utc
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
        # Only load/compute option features (chain → GEX/walls/flip, tape → OFI)
        # and intraday RV/VRP when a strategy actually consumes them — keeps the
        # S3-only path fast and unchanged.
        self._needs_options = any(getattr(s, "needs_options", False) for s in strategies)
        self._needs_rv = any(getattr(s, "needs_rv", False) for s in strategies)

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
            # DESIGN §2.4 freshness: halt on a gapped feed or malformed bars rather
            # than guess (a NaN/inf price or empty session must not poison equity).
            assert_finite_bars(bars.frame, symbol=sym, day=day)
            assert_no_feed_gap(bars.frame.index, interval)
            sf = self.pipeline.precompute(bars, day)
            frame = bars.frame
            or_known_i = self._orb_known_index(frame.index, sf)
            loaded[sym] = {
                "bars": bars,
                "frame": frame,
                "interval": interval,
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
            if self._needs_options:
                loaded[sym]["tape"] = self.provider.get_option_tape(sym, day)
                loaded[sym]["snaps"] = self._precompute_snapshots(sym, day)

        ref = loaded[symbols[0]]
        index = ref["frame"].index
        n = len(index)
        # All symbols share the same RTH bar grid; the engine indexes every symbol by
        # the same positional ``i``, so a mismatch would silently read symbol B's bar
        # i against symbol A's timestamp i. Guard not just the LENGTH but the actual
        # timestamps — a feed hiccup can shift one symbol by an interval while keeping
        # the same count, which a length-only check would miss.
        for sym in symbols:
            other = loaded[sym]["frame"].index
            if len(other) != n:
                raise ValueError(
                    f"bar index length mismatch for {sym}: {len(other)} != {n} "
                    f"(reference {symbols[0]})"
                )
            if not other.equals(index):
                first_bad = next(
                    (j for j in range(n) if other[j] != index[j]), 0
                )
                raise ValueError(
                    f"bar index timestamp mismatch for {sym} vs {symbols[0]} at "
                    f"position {first_bad}: {other[first_bad]} != {index[first_bad]} "
                    "(symbols must share one RTH bar grid)"
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
                if pos.meta.get("structured"):
                    # Defined-risk structure (S2): hold to near the 0DTE close, then
                    # settle binary (keep credit inside short strikes, else capped
                    # max loss). The round-trip cost was charged at entry.
                    if ts < flatten_at and i + 1 < n:
                        continue
                    close_now = float(d["close"][i])
                    inside = abs(close_now - pos.meta["center"]) <= pos.meta["short_width"]
                    per_unit = pos.meta["win_per_unit"] if inside else -pos.meta["loss_per_unit"]
                    gross = per_unit * pos.size
                    costs = pos.entry_cost
                    fill_ts = index[i]
                    reason = "settle_inside" if inside else "settle_outside"
                    fill_price = close_now
                else:
                    exit_reason = self._exit_reason(pos, d, i, ts, flatten_at)
                    if exit_reason is None:
                        continue
                    reason = exit_reason
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
                         "CLOSE", costs, reason)
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
        # since the time-stop / 0DTE settle fires at flatten_at). Handle structured
        # and directional correctly so a leftover is never mis-priced.
        for sym in list(positions.keys()):
            pos = positions[sym]
            d = loaded[sym]
            fill_price = float(d["close"][n - 1])
            if pos.meta.get("structured"):
                inside = abs(fill_price - pos.meta["center"]) <= pos.meta["short_width"]
                per_unit = pos.meta["win_per_unit"] if inside else -pos.meta["loss_per_unit"]
                gross = per_unit * pos.size
                costs = pos.entry_cost
                reason = "settle_inside" if inside else "settle_outside"
            else:
                _, exit_cost = conservative_fill(
                    side=pos.side, instrument=pos.instrument, next_open=fill_price,
                    spread=cfg.cost.fallback_spread_pct * fill_price, size=pos.size,
                    adv=pos.meta.get("adv"), config=cfg.cost, is_entry=False,
                )
                gross = pos.side.sign * (fill_price - pos.entry_price) * pos.size * pos.multiplier
                costs = pos.entry_cost + exit_cost
                reason = "eod_safety"
            net = gross - costs
            day_realized += net
            equity += net
            trades.append(
                Trade(symbol=sym, strategy_id=pos.strategy_id, side=pos.side,
                      size=pos.size, instrument=pos.instrument, entry_ts=pos.entry_ts,
                      exit_ts=index[n - 1], entry_price=pos.entry_price,
                      exit_price=fill_price, gross_pnl=gross, costs=costs,
                      net_pnl=net, exit_reason=reason)
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

    def _precompute_snapshots(self, sym: str, day: date) -> list[tuple]:
        """Per-snapshot (available_ts, GammaStructure|None, atm_iv|None).

        The dealer GEX/flip/walls solve is the expensive part (SWE re-prices the
        chain across many spots to locate the flip), so we recompute it only every
        ``gex_recompute_min`` minutes (the regime is slow-moving), reusing the
        latest structure between. Each result is PIT-stamped by its snapshot's
        ``available_ts`` so no value is used before it has arrived."""
        chain = self.provider.get_option_chain(sym, day)
        avail = sorted(pd.Timestamp(a) for a in pd.unique(chain.frame["available_ts"]))
        if not avail:
            return []
        step = pd.Timedelta(minutes=self.config.data.gex_recompute_min)
        out: list[tuple] = []
        last_at = None
        for at in avail:
            if last_at is not None and (at - last_at) < step:
                continue
            last_at = at
            gs = gamma_structure_at(
                chain, at, expiry=day, ticker=sym,
                risk_free_rate=self.config.gate.risk_free_rate,
            )
            out.append((at, gs, atm_iv_at(chain, at)))
        return out

    @staticmethod
    def _latest_snap(snaps: list[tuple], as_of: pd.Timestamp):
        """The most recent snapshot tuple available at ``as_of`` (PIT)."""
        chosen = None
        for at, gs, iv in snaps:
            if at <= as_of:
                chosen = (gs, iv)
            else:
                break
        return chosen

    def _feature_row(self, sym: str, as_of: pd.Timestamp, d: dict, i: int) -> FeatureRow:
        dev = d["dev"][i]
        sigma = d["sigma"][i]
        orb_known = i >= d["or_known_i"]
        sf = d["sf"]

        ofi = atm_iv = rv = vrp = gex_total = flip = flip_dist = ncw = npw = None
        regime: GammaRegime | None = None
        if "snaps" in d:
            ofi = ofi_at(d["tape"], as_of, self.pipeline.ofi_lookback)
            snap = self._latest_snap(d["snaps"], as_of)
            if snap is not None:
                gs, atm_iv = snap
                if gs is not None:
                    gex_total = gs.gex_total
                    regime = gs.regime
                    flip = gs.flip_level
                    flip_dist = gs.flip_distance_pct
                    ncw, npw = gs.nearest_call_wall, gs.nearest_put_wall
            if self._needs_rv:
                # Trailing window only: every SWE RV estimator uses just .tail(window)
                # / .tail(window+1), so passing the last window+1 bars is identical to
                # the full session prefix but turns the per-tick cost from O(i) into
                # O(window) (O(T*window) over the session instead of O(T^2)).
                rv_window = self.pipeline.rv_window
                rv = intraday_rv(
                    d["frame"].iloc[max(0, i - rv_window) : i + 1], d["interval"],
                    window=rv_window, estimator=self.pipeline.rv_estimator,
                    session=self.config.session,
                )
                if rv is not None and atm_iv is not None:
                    vrp = atm_iv - rv

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
            ofi=ofi,
            rv=rv,
            atm_iv=atm_iv,
            vrp=vrp,
            gex_total=gex_total,
            gamma_regime=regime,
            flip_level=flip,
            flip_distance_pct=flip_dist,
            nearest_call_wall=ncw,
            nearest_put_wall=npw,
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

            if prop.is_structured:
                # Defined-risk structure (S2): entered "now" for the gate's
                # round-trip cost; settled at the 0DTE close (no underlying fill).
                entry_cost = gres.cost.total
                pos = Position(
                    symbol=sym, strategy_id=prop.strategy_id, side=prop.side,
                    size=sizing.size, instrument=prop.instrument,
                    decision_ts=fr.as_of, entry_ts=fr.as_of, entry_price=prop.ref_price,
                    target_price=prop.target_price, stop_price=prop.stop_price,
                    entry_cost=entry_cost, flatten_at=flatten_at,
                    meta={
                        "structured": True,
                        "center": prop.meta["center"],
                        "short_width": prop.meta["short_width"],
                        "win_per_unit": prop.win_amount,
                        "loss_per_unit": prop.loss_amount,
                    },
                )
                positions[sym] = pos
                fills.append(Fill(fr.as_of, sym, prop.side, sizing.size,
                                  prop.ref_price, "OPEN", entry_cost, prop.strategy_id))
                return True

            # Directional: open at next bar's open with adverse slippage.
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
