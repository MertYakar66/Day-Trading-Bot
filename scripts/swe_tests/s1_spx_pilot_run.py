"""S1 gamma-regime pilot — STAGE 2: run the pilot on real SPX data.

PLAN A (the actual backtest engine, unmodified): IntradayBacktester is driven
through a small composite DataProvider that serves bars from the pilot store
with their true IBKR provenance and chains/tape with their true THETA
provenance (the single-source StoreBackedProvider would otherwise force a
dishonest relabel). One 1-session run per stored day with --strategy s1
semantics (S1GammaRegime, default edge=0.10).

PLAN B (dry-run of S1's decision path, same functions the engine calls):
per 5-minute bar we rebuild the engine's FeatureRow exactly
(IntradayBacktester._feature_row logic: vwap_frame sampling, ofi_at on the
captured-empty tape, gamma_structure_at on the PIT chain) and trace:
  - the dealer-gamma regime / GEX / flip / walls under BOTH expiry
    conventions: the engine's own (expiry = trading day, i.e. it treats every
    chain as 0DTE; SWE floors T at 1 calendar day — audit caveat #2) and the
    chain's TRUE expiry;
  - S1.propose() AS-IS (symbol 'SPX', real OFI) — expected to return None at
    every bar for two structural reasons measured here: (1) S1 refuses
    AssetKind.INDEX symbols by design (it trades the SPY/QQQ proxy), and
    (2) OFI is None because no SPX option tape was ever captured;
  - a clearly-labelled COUNTERFACTUAL: the same FeatureRow re-labelled to a
    stock-kind symbol ('SPXCF') with OFI forced to +/-1 (always-confirming),
    so the downstream path — proposal geometry, kelly_size, ExpectancyGate,
    the default reviewer pipeline, conservative next-bar-open fills, exits —
    is exercised end-to-end with realistic engine costs. This measures an
    UPPER BOUND on S1 activity, not an edge.

Scenarios (PIT mapping per stage-1 docstring):
  A 2026-05-27  chain=20260524 snapshot   PIT-safe   [PRIMARY pilot day]
  B 2026-05-28  chain=20260524 carry      PIT-safe
  C 2026-05-29  chain=20260524 carry      PIT-safe
  D 2026-06-01  chain=20260524 carry      PIT-safe
  E 2026-06-01  chain=20260601 snapshot   DESCRIPTIVE-ONLY (the 20260601 file
      has no underlying_timestamp; its spot 7596.05 sits closer to the 06-01
      close than the 05-29 close, so same-day-EOD leakage cannot be ruled out)
  F 2026-06-02  chain=20260601 snapshot   PIT-safe but PARTIAL session (the
      IBKR capture ends 11:15 ET; 21 bars; dry-run only — the ingest coverage
      guard rightly refuses to pad a fabricated flat afternoon)

Run:
    cd C:/Users/merty/Desktop/Day-Trading-Bot
    .venv/Scripts/python.exe -m scripts.swe_tests.s1_spx_pilot_run
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from intraday.authority.gate import ExpectancyGate
from intraday.authority.reviewers import ReviewContext, default_reviewers
from intraday.backtest.engine import IntradayBacktester
from intraday.backtest.fills import conservative_fill
from intraday.config import EngineConfig
from intraday.contracts import BarSeries, DataSource, OptionChainSeries, Side
from intraday.data.ibkr import payload_to_frame
from intraday.data.provider import DataProvider
from intraday.data.store import ParquetStore
from intraday.data.store_provider import StoreBackedProvider
from intraday.features.base import FeatureRow
from intraday.features.gex import gamma_structure_at
from intraday.features.ofi import ofi_at
from intraday.features.pipeline import FeaturePipeline
from intraday.features.vrp import atm_iv_at
from intraday.risk.sizing import kelly_size
from intraday.signals.s1_gamma_regime import S1GammaRegime
from intraday.timeutils import flatten_time_utc, session_bounds_utc

BOT = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
STORE_ROOT = BOT / "data_raw/swe_tests/store_spx"
CHAIN_DIR = SWE / "data_processed/theta/index_options_chains"
OUT = BOT / "data_raw/swe_tests/s1_pilot"
ET = "America/New_York"


# --------------------------------------------------------------------------- #
# Plan A: composite provider (true per-frame provenance; no engine changes)
# --------------------------------------------------------------------------- #
class CompositePilotProvider(DataProvider):
    """Bars from the store as IBKR; chain/tape from the store as THETA."""

    source = DataSource.FUSED  # per-frame provenance preserved underneath

    def __init__(self, store_root: Path, interval: str) -> None:
        store = ParquetStore(store_root)
        self._bars = StoreBackedProvider(
            store, DataSource.IBKR, symbols=["SPX"], interval=interval
        )
        self._opts = StoreBackedProvider(store, DataSource.THETA)

    def trading_days(self, start: date, end: date) -> list[date]:
        return self._bars.trading_days(start, end)

    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        return self._bars.get_bars(symbol, day, interval)

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        return self._opts.get_option_chain(symbol, day)

    def get_option_tape(self, symbol: str, day: date):
        return self._opts.get_option_tape(symbol, day)


def run_plan_a(cfg: EngineConfig) -> dict:
    provider = CompositePilotProvider(STORE_ROOT, "5m")
    days = provider.trading_days(date(2026, 5, 27), date(2026, 6, 2))
    out: dict = {"days": [str(d) for d in days], "runs": {}}
    for day in days:
        bt = IntradayBacktester(cfg, provider, [S1GammaRegime(edge=0.10)])
        res = bt.run(["SPX"], day, day, "5m")
        out["runs"][str(day)] = {
            "n_days": res.n_days,
            "n_signals_gated": len(res.signals),
            "n_trades": len(res.trades),
            "net_pnl": round(res.final_equity - res.initial_capital, 4),
            "signals": res.signals,
        }
        print(
            f"PLAN A {day}: engine ran end-to-end | gated signals={len(res.signals)} "
            f"trades={len(res.trades)} net_pnl=${res.final_equity - res.initial_capital:.2f}"
        )
    return out


# --------------------------------------------------------------------------- #
# Plan B: per-bar decision trace + counterfactual
# --------------------------------------------------------------------------- #
def load_chain_series(fname: str, snapshot_close_et: str, available_utc: pd.Timestamp) -> tuple[OptionChainSeries, date]:
    df = pd.read_parquet(CHAIN_DIR / fname)
    exp = pd.to_datetime(df["expiration"]).dt.normalize().unique()
    assert len(exp) == 1, f"{fname}: multi-expiry"
    frame = pd.DataFrame(
        {
            "snapshot_ts": pd.Timestamp(snapshot_close_et, tz=ET).tz_convert("UTC"),
            "available_ts": available_utc,
            "expiration": pd.Timestamp(exp[0]),
            "strike": df["strike"].astype(float),
            "option_type": df["right"].astype(str),
            "open_interest": df["open_interest"].astype("int64"),
            "implied_vol": df["iv"].astype(float),
            "spot": df["underlying_price"].astype(float),
        }
    )
    return OptionChainSeries("SPX", frame, DataSource.THETA), pd.Timestamp(exp[0]).date()


def partial_day_bars(day: date, cfg: EngineConfig) -> BarSeries:
    """The truncated 06-02 session, exactly as captured (no padding)."""
    payload = json.loads((BOT / "data_raw/swe_tests/raw_spx/SPX_5m_3mo.json").read_text())
    f = payload_to_frame(payload)  # start-labelled UTC
    f.index = f.index + pd.Timedelta(minutes=5)  # engine convention: close-labelled
    et_dates = f.index.tz_convert(ET).date
    f = f.loc[et_dates == day]
    return BarSeries(
        "SPX", "5m", f, DataSource.IBKR,
        latency=pd.Timedelta(milliseconds=cfg.data.bar_latency_ms),
    )


def gs_dict(gs) -> dict:
    if gs is None:
        return {"regime": None}
    return {
        "regime": gs.regime.value,
        "gex_total_usd_per_pct": gs.gex_total,
        "flip_level": gs.flip_level,
        "flip_distance_pct": gs.flip_distance_pct,
        "nearest_call_wall": gs.nearest_call_wall,
        "nearest_put_wall": gs.nearest_put_wall,
        "confidence": gs.confidence,
        "snapshot_spot": gs.spot,
    }


def run_scenario(
    key: str,
    day: date,
    bars: BarSeries,
    chain: OptionChainSeries,
    true_expiry: date,
    cfg: EngineConfig,
    *,
    pit_safe: bool,
    note: str,
) -> dict:
    pipeline = FeaturePipeline(cfg)
    sf = pipeline.precompute(bars, day)
    frame = bars.frame
    index = frame.index
    n = len(index)
    lat = bars.latency
    opens = frame["open"].to_numpy()
    highs = frame["high"].to_numpy()
    lows = frame["low"].to_numpy()
    closes = frame["close"].to_numpy()
    vwap = sf.vwap["vwap"].to_numpy()
    sigma = sf.vwap["vwap_sigma"].to_numpy()
    dev = sf.vwap["vwap_dev_sigma"].to_numpy()
    flatten_at = flatten_time_utc(day, cfg.session)

    # Empty tape exactly as stored (none was captured) -> OFI.
    from scripts.swe_tests.s1_spx_pilot_build_store import empty_tape_frame
    from intraday.contracts import OptionTape

    tape = OptionTape("SPX", empty_tape_frame(), DataSource.THETA)

    # One EOD snapshot per scenario -> gamma structure computed once at its
    # available_ts (the engine's gex_recompute_min grouping degenerates to one).
    at = pd.Timestamp(chain.frame["available_ts"].iloc[0])
    gs_engine = gamma_structure_at(  # engine convention: expiry = trading day (0DTE-style)
        chain, at, expiry=day, ticker="SPX", risk_free_rate=cfg.gate.risk_free_rate
    )
    gs_true = gamma_structure_at(  # chain's true expiry
        chain, at, expiry=true_expiry, ticker="SPX", risk_free_rate=cfg.gate.risk_free_rate
    )
    iv_atm = atm_iv_at(chain, at)

    s1 = S1GammaRegime(edge=0.10)
    gate = ExpectancyGate(cfg)
    reviewers = default_reviewers(cfg)

    def feature_row(i: int, as_of: pd.Timestamp, gs, symbol: str, ofi_val) -> FeatureRow:
        sg = sigma[i]
        dv = dev[i]
        return FeatureRow(
            symbol=symbol,
            as_of=as_of,
            last_price=float(closes[i]),
            vwap=float(vwap[i]),
            vwap_sigma=None if not np.isfinite(sg) else float(sg),
            vwap_dev_sigma=None if not np.isfinite(dv) else float(dv),
            orb_high=None, orb_low=None, orb_volume=None,  # S1 does not read ORB
            ofi=ofi_val,
            rv=None, atm_iv=iv_atm, vrp=None,
            gex_total=None if gs is None else gs.gex_total,
            gamma_regime=None if gs is None else gs.regime,
            flip_level=None if gs is None else gs.flip_level,
            flip_distance_pct=None if gs is None else gs.flip_distance_pct,
            nearest_call_wall=None if gs is None else gs.nearest_call_wall,
            nearest_put_wall=None if gs is None else gs.nearest_put_wall,
            meta={},
        )

    # ---- as-is per-bar trace + counterfactual sims under both conventions --
    rows = []
    asis_proposals = 0
    cf: dict[str, dict] = {}
    for conv, gs in (("engine_0dte_conv", gs_engine), ("true_expiry", gs_true)):
        cf[conv] = {
            "n_proposals": 0, "verdicts": {}, "gate_records": [], "trades": [],
            "net_pnl": 0.0, "position": None, "day_realized": 0.0, "killed": False,
        }

    nav0 = cfg.risk.paper_nav
    budget = cfg.risk.daily_loss_limit_pct * nav0

    for i in range(n):
        ts = index[i]
        as_of = ts + lat
        ofi_real = ofi_at(tape, as_of, pipeline.ofi_lookback)  # None: no tape captured

        # AS-IS S1 (symbol SPX, real OFI). Expected None every bar.
        fr_asis = feature_row(i, as_of, gs_engine, "SPX", ofi_real)
        if s1.propose(fr_asis, config=cfg) is not None:
            asis_proposals += 1

        # diagnostics row (engine-convention regime + true-expiry regime)
        rows.append(
            {
                "ts_et": str(ts.tz_convert(ET)),
                "close": closes[i],
                "vwap": vwap[i],
                "vwap_dev_sigma": dev[i],
                "ofi_real": ofi_real,
                "regime_engine_conv": None if gs_engine is None else gs_engine.regime.value,
                "flip_engine_conv": None if gs_engine is None else gs_engine.flip_level,
                "regime_true_expiry": None if gs_true is None else gs_true.regime.value,
                "flip_true_expiry": None if gs_true is None else gs_true.flip_level,
                "above_flip_engine": (
                    None if (gs_engine is None or gs_engine.flip_level is None)
                    else bool(closes[i] > gs_engine.flip_level)
                ),
            }
        )

        # COUNTERFACTUAL sims (stock-kind symbol + always-confirming OFI).
        for conv, gs in (("engine_0dte_conv", gs_engine), ("true_expiry", gs_true)):
            st = cf[conv]

            # exits first (engine order)
            pos = st["position"]
            if pos is not None:
                reason = None
                if pos["side"] is Side.LONG:
                    if lows[i] <= pos["stop"]:
                        reason = "stop"
                    elif highs[i] >= pos["target"]:
                        reason = "target"
                else:
                    if highs[i] >= pos["stop"]:
                        reason = "stop"
                    elif lows[i] <= pos["target"]:
                        reason = "target"
                if reason is None and ts >= flatten_at:
                    reason = "time_stop"
                if reason is None and i == n - 1:
                    reason = "capture_end"  # partial-session force-close (not engine semantics)
                if reason is not None:
                    ref_open = float(opens[i + 1]) if i + 1 < n else float(closes[i])
                    fill_ts = index[i + 1] if i + 1 < n else index[i]
                    fill_price, exit_cost = conservative_fill(
                        side=pos["side"], instrument=pos["instrument"], next_open=ref_open,
                        spread=cfg.cost.fallback_spread_pct * ref_open, size=pos["size"],
                        adv=None, config=cfg.cost, is_entry=False,
                    )
                    gross = pos["side"].sign * (fill_price - pos["entry_price"]) * pos["size"]
                    net = gross - (pos["entry_cost"] + exit_cost)
                    st["day_realized"] += net
                    st["net_pnl"] += net
                    st["trades"].append(
                        {
                            "side": pos["side"].value, "size": pos["size"],
                            "entry_ts_et": str(pos["entry_ts"].tz_convert(ET)),
                            "exit_ts_et": str(fill_ts.tz_convert(ET)),
                            "entry_price": pos["entry_price"], "exit_price": fill_price,
                            "target": pos["target"], "stop": pos["stop"],
                            "gross_pnl": round(gross, 4),
                            "costs": round(pos["entry_cost"] + exit_cost, 4),
                            "net_pnl": round(net, 4), "exit_reason": reason,
                        }
                    )
                    st["position"] = None
                    if budget > 0 and st["day_realized"] <= -budget:
                        st["killed"] = True

            # entries (engine guards: next bar must exist, before flatten)
            if st["position"] is not None or i + 1 >= n or ts >= flatten_at:
                continue
            prop = None
            for ofi_cf in (1.0, -1.0):  # always-confirming order flow (counterfactual)
                fr_cf = feature_row(i, as_of, gs, "SPXCF", ofi_cf)
                prop = s1.propose(fr_cf, config=cfg)
                if prop is not None:
                    break
            if prop is None:
                continue
            st["n_proposals"] += 1
            sizing = kelly_size(prop, cfg.risk)
            if sizing.size <= 0:
                st["verdicts"]["unsized"] = st["verdicts"].get("unsized", 0) + 1
                continue
            gres = gate.evaluate(prop, sizing.size)
            ctx = ReviewContext(
                as_of=as_of, symbol="SPXCF", feature_row=fr_cf,
                daily_realized_pnl=st["day_realized"], nav=nav0,
                session_killed=st["killed"],
            )
            gres = reviewers.apply(gres, ctx)
            st["verdicts"][gres.verdict.name] = st["verdicts"].get(gres.verdict.name, 0) + 1
            if len(st["gate_records"]) < 12:
                st["gate_records"].append(
                    {
                        "ts_et": str(ts.tz_convert(ET)),
                        "side": prop.side.value,
                        "regime": prop.meta["regime"],
                        "ref": prop.ref_price, "target": prop.target_price,
                        "stop": prop.stop_price, "win_prob": round(prop.win_prob, 4),
                        "size": sizing.size,
                        "ev_gross": round(gres.ev_gross, 2),
                        "cost_total": round(gres.cost.total, 2),
                        "ev_net": round(gres.ev_net, 2),
                        "verdict": gres.verdict.name, "reason": gres.reason,
                        "trail": list(gres.trail),
                    }
                )
            if not gres.tradeable:
                continue
            nxt_open = float(opens[i + 1])
            fill_price, entry_cost = conservative_fill(
                side=prop.side, instrument=prop.instrument, next_open=nxt_open,
                spread=prop.spread, size=sizing.size, adv=prop.adv,
                config=cfg.cost, is_entry=True,
            )
            st["position"] = {
                "side": prop.side, "size": sizing.size, "instrument": prop.instrument,
                "entry_ts": index[i + 1], "entry_price": fill_price,
                "target": prop.target_price, "stop": prop.stop_price,
                "entry_cost": entry_cost,
            }

    trace = pd.DataFrame(rows)
    trace_path = OUT / f"trace_{key}_{day}.csv"
    trace.to_csv(trace_path, index=False)

    day_lo, day_hi = float(np.min(lows)), float(np.max(highs))
    summary = {
        "scenario": key,
        "day": str(day),
        "pit_safe": pit_safe,
        "note": note,
        "n_bars": n,
        "session_low": day_lo,
        "session_high": day_hi,
        "session_close_last_bar": float(closes[-1]),
        "chain_snapshot_spot": float(chain.frame["spot"].iloc[0]),
        "chain_true_expiry": str(true_expiry),
        "atm_iv": iv_atm,
        "gamma_engine_0dte_conv": gs_dict(gs_engine),
        "gamma_true_expiry": gs_dict(gs_true),
        "s1_asis": {
            "n_proposals": asis_proposals,
            "blockers": [
                "S1 returns None for AssetKind.INDEX symbols (SPX is context-only by design; it trades the SPY/QQQ proxy)",
                "OFI is None on every bar (no SPX option tape was captured), which alone forces propose()->None",
            ],
        },
        "s1_counterfactual": {
            conv: {
                "n_proposals": cf[conv]["n_proposals"],
                "verdicts": cf[conv]["verdicts"],
                "n_trades": len(cf[conv]["trades"]),
                "net_pnl_usd": round(cf[conv]["net_pnl"], 2),
                "trades": cf[conv]["trades"],
                "gate_records_first": cf[conv]["gate_records"],
            }
            for conv in cf
        },
        "trace_csv": str(trace_path),
    }
    return summary


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = EngineConfig.default()
    store = ParquetStore(STORE_ROOT)

    print("=" * 76)
    print("PLAN A — actual IntradayBacktester, S1, composite store provider")
    print("=" * 76)
    plan_a = run_plan_a(cfg)

    print()
    print("=" * 76)
    print("PLAN B — per-bar decision trace + counterfactual")
    print("=" * 76)

    def store_bars(day: date) -> BarSeries:
        return store.read_bars("SPX", day, "5m")

    def open_utc(day: date) -> pd.Timestamp:
        return session_bounds_utc(day)[0]

    scenarios = []
    # A-D: 20260524 snapshot (inferred 05-22 16:00 ET close), PIT-safe.
    for key, day, note in (
        ("A_primary", date(2026, 5, 27), "PRIMARY pilot day for the 20260524 snapshot (05-26 has no bars; chain is 3 sessions stale)"),
        ("B_carry", date(2026, 5, 28), "carry-forward of the 20260524 snapshot"),
        ("C_carry", date(2026, 5, 29), "carry-forward of the 20260524 snapshot"),
        ("D_carry", date(2026, 6, 1), "carry-forward; 20260601 snapshot conservatively unavailable intraday"),
    ):
        chain, exp = load_chain_series("SPX_20260524.parquet", "2026-05-22 16:00", open_utc(day))
        scenarios.append(run_scenario(key, day, store_bars(day), chain, exp, cfg, pit_safe=True, note=note))

    # E: descriptive same-day run of the 20260601 snapshot on 06-01.
    chain_e, exp_e = load_chain_series("SPX_20260601.parquet", "2026-06-01 16:00", open_utc(date(2026, 6, 1)))
    scenarios.append(
        run_scenario(
            "E_descriptive", date(2026, 6, 1), store_bars(date(2026, 6, 1)), chain_e, exp_e, cfg,
            pit_safe=False,
            note="DESCRIPTIVE ONLY: 20260601 snapshot used on 06-01 itself; same-day EOD leakage cannot be ruled out (file has no underlying_timestamp; spot matches the 06-01 close better than the 05-29 close)",
        )
    )

    # F: PIT-safe pilot day for the 20260601 snapshot — the partial 06-02 capture.
    bars_f = partial_day_bars(date(2026, 6, 2), cfg)
    chain_f, exp_f = load_chain_series("SPX_20260601.parquet", "2026-06-01 16:00", open_utc(date(2026, 6, 2)))
    scenarios.append(
        run_scenario(
            "F_partial", date(2026, 6, 2), bars_f, chain_f, exp_f, cfg,
            pit_safe=True,
            note="PIT-safe pilot day for the 20260601 snapshot, but the capture ends 11:15 ET (21/78 bars); positions force-closed at capture end; engine (Plan A) rightly refuses this session at 27% coverage",
        )
    )

    for s in scenarios:
        ge = s["gamma_engine_0dte_conv"]
        gt = s["gamma_true_expiry"]
        print(
            f"{s['scenario']:14s} {s['day']} pit_safe={s['pit_safe']} | regime(engine-conv)={ge['regime']} "
            f"GEX=${ge.get('gex_total_usd_per_pct', 0):,.0f}/1% flip={ge.get('flip_level')} | "
            f"regime(true-exp)={gt['regime']} flip={gt.get('flip_level')}"
        )
        print(
            f"  as-is proposals={s['s1_asis']['n_proposals']} | CF(engine-conv): "
            f"props={s['s1_counterfactual']['engine_0dte_conv']['n_proposals']} "
            f"verdicts={s['s1_counterfactual']['engine_0dte_conv']['verdicts']} "
            f"trades={s['s1_counterfactual']['engine_0dte_conv']['n_trades']} "
            f"netPnL=${s['s1_counterfactual']['engine_0dte_conv']['net_pnl_usd']}"
        )

    results = {
        "workstream": "S1 gamma-regime pilot on real SPX data",
        "n_sessions_with_bars": 5,
        "n_sessions_engine_runnable": 4,
        "n_chain_snapshots": 3,
        "n_chain_snapshots_usable": 2,
        "unusable_snapshot": {
            "file": "SPX_20260423.parquet",
            "underlying_timestamp": "2026-04-22T16:05:05 (ET semantics; just after the 04-22 close)",
            "reason": "would trade 2026-04-23/24, but the 'SPX_5m_3mo' IBKR capture actually holds only ONE WEEK (2026-05-27..06-02) due to the 1000-bar MCP cap fallback",
        },
        "plan_a": plan_a,
        "plan_b_scenarios": scenarios,
    }
    out_path = OUT / "s1_pilot_results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nresults written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
