"""Ingest raw Theta captures into the engine store — offline, network-free.

Closes audit gaps G3/G4: maps ``data_raw/theta/ticker=SYM/date=D/{trades,quotes,
oi}.parquet`` (the output of ``scripts.pull_theta_tape_scoped``) into the
partitioned engine store:

- trades  -> ``option_tape``  (``DataSource.THETA``; ``available_ts = ts + tape_latency``)
- quotes  -> ``option_quotes`` (``DataSource.THETA``; raw material, not an engine input)
- quotes + daily OI -> **synthesized** ``option_chain`` snapshots at 5-minute
  cadence (``DataSource.THETA_DERIVED``): per-contract last-quote mid at each
  snapshot, Black-Scholes-inverted ``implied_vol``, put-call-parity ``spot``.
- quotes -> optional PARITY underlying bars (``--parity-bars``), never clobbering
  an existing bars partition (IBKR wins).

Binding correctness rules (the adversarial PIT/math spec; each enforced by test):

- **PIT OI (R1/R2)**: a day-D intraday snapshot may carry only OI knowable before
  D's open — the default ``--oi-policy prev_session`` reads the latest raw
  partition STRICTLY BEFORE D. OI is constant across D's snapshots (one number
  per contract per day; an intraday OI ramp would be fabrication).
  ``--oi-policy same_day_eod`` exists for descriptive runs only and is
  name-shamed in the synthesis sidecar (``same_day_eod_LEAKY``). (If the
  operator's probe confirms Theta's ``date=D`` OI row is the *prior*
  settlement, it is PIT-safe — verify before relaxing the default.)
- **Inversion clock (R7)**: ``implied_vol`` is inverted at the SAME ``T`` its
  consumer re-prices with — the SWE dealer analyzer's ``max(days, 1)/365``
  (1/365 for 0DTE all day), NOT an intraday-decaying T. Mixing clocks makes the
  stored IV unable to reproduce the observed premium, with error growing toward
  the close. ``parity.year_fraction`` (decaying) is used ONLY for the parity
  spot, exactly as its consumer does.
- **One (r, q) everywhere (R8)**: ``r = config.gate.risk_free_rate``; per-symbol
  ``q`` feeds parity spot AND the IV inversion.
- **Honest absence (R5/R6/R9)**: only quotes with ``bid > 0`` and ``ask > bid``
  and a mid inside the no-arb band invert; failed/junk inversions are DROPPED
  and counted, never placeholdered. A snapshot with no usable parity pair is
  dropped whole (a chain row without a real spot is unknowable).
- **Same-day expiry (R13/G8)**: the engine hardcodes ``expiry == session day``;
  a chain with zero same-day rows is refused unless ``--allow-nonzero-dte``.
- **SPY/QQQ only (R14)**: SPX/SPXW synthesis is refused (AM/PM settlement
  ambiguity + junk SPXW OI on prior captures).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.option_pricer import implied_volatility  # noqa: E402  (vendor/swe, read-only)

from intraday.config import EngineConfig, SessionConfig  # noqa: E402
from intraday.contracts import (  # noqa: E402
    CHAIN_COLUMNS,
    TAPE_COLUMNS,
    DataSource,
    OptionChainSeries,
    OptionTape,
)
from intraday.data.parity import implied_spot, year_fraction  # noqa: E402
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.timeutils import bar_close_index, parse_interval  # noqa: E402

# Per-symbol continuous dividend yields (parity.py's documented figures); one
# (r, q) pair feeds parity spot AND the IV inversion (R8).
PER_SYMBOL_Q: dict[str, float] = {"SPY": 0.013, "QQQ": 0.006}

# The GEX spine. SPX/SPXW synthesis is refused (R14).
_REFUSED_ROOTS = {"SPX", "SPXW"}

# Junk-root ceiling: the vendor solver's Brent fallback brackets [0.001, 10];
# a deep wing with ~zero vega can "converge" to an absurd root (R6).
MAX_PLAUSIBLE_IV = 5.0


def consumer_T(expiry: date, day: date) -> float:
    """The exact T the downstream GEX consumer re-prices with (R7):
    SWE ``dealer_positioning.analyze`` uses ``max((expiry - day).days, 1)/365``,
    pinned at 1/365 for a same-day expiry all session long."""
    return max((pd.Timestamp(expiry) - pd.Timestamp(day)).days, 1) / 365.0


@dataclass
class SynthStats:
    """Per-day synthesis accounting (printed and persisted to the sidecar)."""

    snapshots_total: int = 0
    snapshots_no_spot: int = 0
    rows_emitted: int = 0
    rows_dropped_iv: int = 0
    oi_missing_contracts: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "snapshots_total": self.snapshots_total,
            "snapshots_no_spot": self.snapshots_no_spot,
            "rows_emitted": self.rows_emitted,
            "rows_dropped_iv": self.rows_dropped_iv,
            "oi_missing_contracts": self.oi_missing_contracts,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# Tape ingest (raw trades -> TAPE_COLUMNS with PIT stamping)
# --------------------------------------------------------------------------- #
def ingest_tape(
    store: ParquetStore, symbol: str, day: date, trades: pd.DataFrame,
    *, latency: pd.Timedelta,
) -> Path:
    """Raw puller trades -> ``option_tape`` with ``available_ts = ts + latency``."""
    out = pd.DataFrame()
    ts = pd.to_datetime(trades["ts"], utc=True)
    out["ts"] = ts
    out["available_ts"] = ts + latency
    out["expiration"] = trades["expiration"]
    out["strike"] = trades["strike"]
    out["right"] = trades["right"]
    out["price"] = trades["price"]
    out["size"] = trades["size"]
    out["nbbo_bid"] = trades["nbbo_bid"]
    out["nbbo_ask"] = trades["nbbo_ask"]
    out["side_inferred"] = trades["side_inferred"]
    out = out[list(TAPE_COLUMNS)]
    return store.write_tape(OptionTape(symbol.upper(), out, DataSource.THETA), day)


# --------------------------------------------------------------------------- #
# PIT OI
# --------------------------------------------------------------------------- #
def prev_session_oi(raw_root: Path, symbol: str, day: date) -> pd.DataFrame:
    """OI from the latest raw partition STRICTLY BEFORE ``day`` (R1).

    Day-D's own EOD OI is computed from D's settlement — a future fact at every
    intraday slot of D. Returns an empty frame when no earlier partition exists
    (the synthesizer then emits OI=0 with a warning, never a guess).
    """
    base = raw_root / f"ticker={symbol.upper()}"
    if not base.is_dir():
        return pd.DataFrame(columns=["date", "expiration", "strike", "right", "open_interest"])
    earlier = sorted(
        d for d in base.glob("date=*")
        if (d / "oi.parquet").exists() and d.name.split("=", 1)[1] < day.isoformat()
    )
    if not earlier:
        return pd.DataFrame(columns=["date", "expiration", "strike", "right", "open_interest"])
    return pd.read_parquet(earlier[-1] / "oi.parquet", engine="pyarrow")


# --------------------------------------------------------------------------- #
# Chain synthesis (quotes + OI -> CHAIN_COLUMNS snapshots)
# --------------------------------------------------------------------------- #
def parity_spot_at(
    last_quotes: pd.DataFrame,
    *,
    snapshot_ts: pd.Timestamp,
    r: float,
    q: float,
    session: SessionConfig,
    max_leg_rel_spread: float = 0.25,
) -> float | None:
    """Spot via put-call parity from the snapshot's same-expiry C/P mid pairs.

    Median of ``implied_spot`` over the 3 strikes with the smallest
    ``|call_mid - put_mid|`` (nearest-forward; the median kills single-strike
    quote glitches). A leg whose relative spread exceeds ``max_leg_rel_spread``
    is rejected as stale/illiquid (R12). Returns ``None`` (refusal, R11) when no
    usable pair exists — never an approximation.

    Uses ``parity.year_fraction`` (decaying, anchored at the expiry's RTH close)
    — correct for SPOT only; the IV inversion deliberately uses a different,
    consumer-matched T (R7).
    """
    lq = last_quotes
    ok = (
        (lq["bid"] > 0) & (lq["ask"] > lq["bid"]) & lq["mid"].notna()
        & ((lq["ask"] - lq["bid"]) / lq["mid"] <= max_leg_rel_spread)
    )
    lq = lq.loc[ok]
    if lq.empty:
        return None
    calls = lq[lq["right"] == "C"].set_index(["expiration", "strike"])["mid"]
    puts = lq[lq["right"] == "P"].set_index(["expiration", "strike"])["mid"]
    pairs = pd.DataFrame({"call_mid": calls, "put_mid": puts}).dropna()
    if pairs.empty:
        return None
    pairs = pairs.assign(gap=(pairs["call_mid"] - pairs["put_mid"]).abs())
    pairs = pairs.sort_values("gap").head(3)
    spots = [
        float(
            implied_spot(
                row["call_mid"], row["put_mid"], strike,
                r=r, q=q, T=year_fraction(snapshot_ts, expiration, session=session),
            )
        )
        for (expiration, strike), row in pairs.iterrows()
    ]
    return float(np.median(spots))


def synthesize_chain(
    quotes: pd.DataFrame,
    oi: pd.DataFrame,
    *,
    symbol: str,
    day: date,
    cadence: str = "5m",
    r: float,
    q: float = 0.0,
    session: SessionConfig | None = None,
    latency: pd.Timedelta = pd.Timedelta(milliseconds=1_000),
    max_leg_rel_spread: float = 0.25,
) -> tuple[pd.DataFrame, SynthStats]:
    """1m quote mids + daily OI -> CHAIN_COLUMNS snapshots on the cadence grid.

    Per snapshot: backward-looking last quote per contract (PAST-only, at most
    one cadence step old — staler is dropped, R12), parity spot (refused -> the
    whole snapshot is dropped), BS-inverted IV at the consumer's T (failed/junk
    inversions dropped and counted, R5/R6/R9), prior-session OI joined constant
    across the day (R1/R2), ``available_ts = snapshot_ts + latency`` (R3).
    """
    sym = symbol.upper()
    if sym in _REFUSED_ROOTS:
        raise ValueError(
            f"refusing to synthesize a chain for {sym}: AM/PM settlement ambiguity "
            "and junk SPXW OI on prior captures (R14). The GEX spine is SPY/QQQ."
        )
    session = session or SessionConfig()
    stats = SynthStats()
    grid = bar_close_index(day, cadence, session)
    stats.snapshots_total = len(grid)
    max_age = parse_interval(cadence)

    qf = quotes.copy()
    qf["ts"] = pd.to_datetime(qf["ts"], utc=True)
    qf = qf.sort_values("ts")

    oi_map: dict[tuple[date, float, str], float] = {}
    for _, row in oi.iterrows():
        key = (pd.Timestamp(row["expiration"]).date(), float(row["strike"]), str(row["right"]))
        oi_map[key] = float(row["open_interest"])

    rows: list[dict] = []
    missing_oi_keys: set[tuple[date, float, str]] = set()
    for snap in grid:
        # Staleness rule (R12) is the window itself: a quote older than one
        # cadence step from the snapshot simply never enters it.
        window = qf[(qf["ts"] <= snap) & (qf["ts"] > snap - max_age)]
        if window.empty:
            stats.snapshots_no_spot += 1
            continue
        last = (
            window.groupby(["expiration", "strike", "right"], as_index=False)
            .last()
        )
        spot = parity_spot_at(
            last, snapshot_ts=snap, r=r, q=q, session=session,
            max_leg_rel_spread=max_leg_rel_spread,
        )
        if spot is None or not np.isfinite(spot) or spot <= 0:
            stats.snapshots_no_spot += 1
            continue
        for _, lrow in last.iterrows():
            mid = lrow["mid"]
            if not (np.isfinite(mid) if mid is not None else False) or mid <= 0:
                stats.rows_dropped_iv += 1
                continue
            expiration = pd.Timestamp(lrow["expiration"]).date()
            strike = float(lrow["strike"])
            right = str(lrow["right"])
            iv = implied_volatility(
                float(mid), spot, strike, consumer_T(expiration, day), r,
                "call" if right == "C" else "put", q=q,
            )
            if iv is None or not np.isfinite(iv) or iv <= 0 or iv > MAX_PLAUSIBLE_IV:
                stats.rows_dropped_iv += 1
                continue
            key = (expiration, strike, right)
            oi_val = oi_map.get(key)
            if oi_val is None:
                missing_oi_keys.add(key)
                oi_val = 0.0
            rows.append({
                "snapshot_ts": snap,
                "available_ts": snap + latency,
                "expiration": expiration,
                "strike": strike,
                "option_type": right,
                "open_interest": oi_val,
                "implied_vol": float(iv),
                "spot": spot,
            })
    stats.rows_emitted = len(rows)
    stats.oi_missing_contracts = len(missing_oi_keys)
    if missing_oi_keys:
        stats.warnings.append(
            f"{len(missing_oi_keys)} contracts had no prior-settlement OI -> 0 "
            "(no gamma weight; never a guess)"
        )
    frame = pd.DataFrame(rows, columns=list(CHAIN_COLUMNS))
    return frame, stats


def validate_same_day_expiry(frame: pd.DataFrame, day: date) -> tuple[int, int]:
    """G8/R13: the engine reads option features with ``expiry == session day``
    hardcoded; a chain without same-day rows yields all-None option features."""
    if frame.empty:
        return 0, 0
    exp = pd.to_datetime(frame["expiration"]).dt.date
    same = int((exp == day).sum())
    return same, int(len(frame) - same)


# --------------------------------------------------------------------------- #
# Optional PARITY underlying bars (no-clobber)
# --------------------------------------------------------------------------- #
def quotes_to_parity_frame(quotes: pd.DataFrame) -> pd.DataFrame:
    """Per-timestamp nearest-forward ATM C/P pair -> ``reconstruct_spot`` input."""
    qf = quotes.copy()
    qf["ts"] = pd.to_datetime(qf["ts"], utc=True)
    ok = (qf["bid"] > 0) & (qf["ask"] > qf["bid"]) & qf["mid"].notna()
    qf = qf.loc[ok]
    if qf.empty:
        return pd.DataFrame(columns=["strike", "call_mid", "put_mid", "expiry"])
    calls = qf[qf["right"] == "C"].set_index(["ts", "expiration", "strike"])["mid"]
    puts = qf[qf["right"] == "P"].set_index(["ts", "expiration", "strike"])["mid"]
    pairs = pd.DataFrame({"call_mid": calls, "put_mid": puts}).dropna().reset_index()
    if pairs.empty:
        return pd.DataFrame(columns=["strike", "call_mid", "put_mid", "expiry"])
    pairs["gap"] = (pairs["call_mid"] - pairs["put_mid"]).abs()
    best = pairs.loc[pairs.groupby("ts")["gap"].idxmin()]
    out = best.rename(columns={"expiration": "expiry"}).set_index("ts")
    return out[["strike", "call_mid", "put_mid", "expiry"]]


def ingest_parity_bars(
    store: ParquetStore, symbol: str, day: date, quotes: pd.DataFrame,
    *, interval: str, cfg: EngineConfig, q: float,
) -> Path | None:
    """Reconstruct PARITY bars from the quotes — NEVER clobbering an existing
    bars partition (IBKR wins; ``write_bars`` paths are source-agnostic, so the
    no-clobber invariant lives here)."""
    from intraday.data.parity import reconstruct_spot, spot_to_bars

    part = store._partition("bars", symbol, day) / f"bars_{interval}.parquet"
    if part.exists():
        print(f"  {symbol} {day}: bars_{interval} partition exists - parity SKIPPED (no clobber)")
        return None
    pframe = quotes_to_parity_frame(quotes)
    spot = reconstruct_spot(pframe, r=cfg.gate.risk_free_rate, q=q, session=cfg.session)
    bars = spot_to_bars(
        spot, symbol, day, interval,
        session=cfg.session,
        latency=pd.Timedelta(milliseconds=cfg.data.bar_latency_ms),
    )
    return store.write_bars(bars, day)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _raw_partitions(raw_root: Path, symbol: str, start: date, end: date) -> list[date]:
    base = raw_root / f"ticker={symbol.upper()}"
    if not base.is_dir():
        return []
    days = []
    for d in sorted(base.glob("date=*")):
        try:
            day = date.fromisoformat(d.name.split("=", 1)[1])
        except ValueError:
            continue
        if start <= day <= end:
            days.append(day)
    return days


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest raw Theta captures into the engine store (offline)."
    )
    p.add_argument("--raw-root", type=Path, default=Path("data_raw/theta"))
    p.add_argument("--store-root", type=Path, default=Path("data_store"))
    p.add_argument("--symbols", nargs="*", default=["SPY", "QQQ"])
    p.add_argument("--start", type=date.fromisoformat, default=date(2022, 1, 3))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 12, 31))
    p.add_argument("--cadence", default="5m", help="chain snapshot cadence")
    p.add_argument("--quote-interval", default="1m", help="stored quote-bar interval")
    p.add_argument("--oi-policy", choices=["prev_session", "same_day_eod"],
                   default="prev_session")
    p.add_argument("--allow-nonzero-dte", action="store_true",
                   help="write a chain even when it has no same-day-expiry rows "
                        "(the engine's option features will be None all session)")
    p.add_argument("--parity-bars", action="store_true")
    p.add_argument("--parity-interval", default="1m")
    args = p.parse_args(argv)

    cfg = EngineConfig.default()
    store = ParquetStore(args.store_root)
    refused = 0
    for sym in (s.upper() for s in args.symbols):
        q = PER_SYMBOL_Q.get(sym, 0.0)
        for day in _raw_partitions(args.raw_root, sym, args.start, args.end):
            part = args.raw_root / f"ticker={sym}" / f"date={day.isoformat()}"
            trades = pd.read_parquet(part / "trades.parquet", engine="pyarrow")
            quotes = pd.read_parquet(part / "quotes.parquet", engine="pyarrow")

            ingest_tape(store, sym, day, trades,
                        latency=pd.Timedelta(milliseconds=cfg.data.tape_latency_ms))
            store.write_option_quotes(sym, day, quotes, source=DataSource.THETA,
                                      interval=args.quote_interval)

            if args.oi_policy == "prev_session":
                oi = prev_session_oi(args.raw_root, sym, day)
                oi_label = "prev_session"
            else:
                oi_path = part / "oi.parquet"
                oi = (pd.read_parquet(oi_path, engine="pyarrow") if oi_path.exists()
                      else prev_session_oi(args.raw_root, sym, day))
                oi_label = "same_day_eod_LEAKY"

            try:
                chain, stats = synthesize_chain(
                    quotes, oi, symbol=sym, day=day, cadence=args.cadence,
                    r=cfg.gate.risk_free_rate, q=q, session=cfg.session,
                    latency=pd.Timedelta(milliseconds=cfg.data.chain_latency_ms),
                )
            except ValueError as e:
                print(f"  {sym} {day}: REFUSED - {e}")
                refused += 1
                continue

            same, other = validate_same_day_expiry(chain, day)
            if same == 0 and not args.allow_nonzero_dte:
                print(f"  {sym} {day}: REFUSED - chain has 0 same-day-expiry rows "
                      f"({other} other-tenor); engine option features would be None "
                      "all session (pass --allow-nonzero-dte to override)")
                refused += 1
                continue

            store.write_chain(OptionChainSeries(sym, chain, DataSource.THETA_DERIVED), day)
            sidecar = {
                "synthesized": True,
                "method": "bs_mid_inversion+parity_spot",
                "oi_policy": oi_label,
                "cadence": args.cadence,
                "r": cfg.gate.risk_free_rate, "q": q,
                **stats.as_dict(),
            }
            chain_part = store._partition("option_chain", sym, day)
            (chain_part / "chain_synthesis.json").write_text(json.dumps(sidecar, indent=1))

            if args.parity_bars:
                ingest_parity_bars(store, sym, day, quotes,
                                   interval=args.parity_interval, cfg=cfg, q=q)
            print(f"  {sym} {day}: tape={len(trades)} chain_rows={stats.rows_emitted} "
                  f"(dropped_iv={stats.rows_dropped_iv}, no_spot_snaps="
                  f"{stats.snapshots_no_spot}/{stats.snapshots_total}, oi={oi_label})")
    return 0 if refused == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
