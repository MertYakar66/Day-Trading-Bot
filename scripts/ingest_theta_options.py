"""Ingest raw Theta captures into the engine store — offline, network-free.

Thin CLI over the pure synthesis core in :mod:`intraday.data.chain_synthesis`
(which documents the binding PIT/math rules). Maps ``data_raw/theta/ticker=SYM/
date=D/{trades,quotes,oi}.parquet`` (the output of
``scripts.pull_theta_tape_scoped``) into the partitioned engine store:

- quotes + daily OI -> **synthesized** ``option_chain`` snapshots
  (``DataSource.THETA_DERIVED``) — validated FIRST: a refused chain (non-0DTE,
  SPX, systematic OI mismatch) writes NOTHING for the session, so the store
  never holds a tape without its chain.
- trades  -> ``option_tape``  (``DataSource.THETA``; ``available_ts = ts + tape_latency``)
- quotes  -> ``option_quotes`` (``DataSource.THETA``)
- quotes  -> optional PARITY underlying bars (``--parity-bars``), never
  clobbering an existing bars partition (IBKR wins).

Failure semantics mirror the puller: per-partition, loud, and the run continues —
one incomplete or corrupt raw partition (e.g. an interrupted pull that never
wrote its manifest) is skipped with a recorded failure, never a crash that
aborts a multi-symbol ingest. Exit code is non-zero if anything was refused or
failed.

OI policy (R1): default ``prev_session`` reads the latest raw partition STRICTLY
BEFORE day D. ``--oi-policy same_day_eod`` exists for descriptive runs only and
is name-shamed in the synthesis sidecar — unless its ``oi.parquet`` is absent
and the code falls back to prev_session, in which case the sidecar says so
accurately. (If the operator's probe confirms Theta's ``date=D`` OI row is the
*prior* settlement, same-day is PIT-safe — verify before relaxing the default.)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intraday.config import EngineConfig  # noqa: E402
from intraday.contracts import (  # noqa: E402
    TAPE_COLUMNS,
    DataSource,
    OptionChainSeries,
    OptionTape,
)
from intraday.data.chain_synthesis import (  # noqa: E402
    PER_SYMBOL_Q,
    quotes_to_parity_frame,
    synthesize_chain,
    validate_same_day_expiry,
)
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.timeutils import trading_days  # noqa: E402

# Warn when the prior-session OI powering a chain is older than this many
# trading sessions (a failed capture week would otherwise silently carry
# two-week-old gamma weights with no audit trail).
OI_STALENESS_WARN_SESSIONS = 3


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
def prev_session_oi(
    raw_root: Path, symbol: str, day: date
) -> tuple[pd.DataFrame, date | None]:
    """OI (and its source session) from the latest raw partition STRICTLY BEFORE
    ``day`` (R1).

    Day-D's own EOD OI is computed from D's settlement — a future fact at every
    intraday slot of D. Returns an empty frame when no earlier partition exists
    (the synthesizer then emits OI=0 with a warning, never a guess).
    """
    empty = pd.DataFrame(columns=["date", "expiration", "strike", "right", "open_interest"])
    base = raw_root / f"ticker={symbol.upper()}"
    if not base.is_dir():
        return empty, None
    earlier = sorted(
        d for d in base.glob("date=*")
        if (d / "oi.parquet").exists() and d.name.split("=", 1)[1] < day.isoformat()
    )
    if not earlier:
        return empty, None
    src_day = date.fromisoformat(earlier[-1].name.split("=", 1)[1])
    return pd.read_parquet(earlier[-1] / "oi.parquet", engine="pyarrow"), src_day


# --------------------------------------------------------------------------- #
# Optional PARITY underlying bars (no-clobber)
# --------------------------------------------------------------------------- #
def ingest_parity_bars(
    store: ParquetStore, symbol: str, day: date, quotes: pd.DataFrame,
    *, interval: str, cfg: EngineConfig, q: float,
) -> Path | None:
    """Reconstruct PARITY bars from the quotes — NEVER clobbering an existing
    bars partition (IBKR wins; ``write_bars`` paths are source-agnostic, so the
    no-clobber invariant lives here)."""
    from intraday.data.parity import reconstruct_spot, spot_to_bars

    if store.has_bars(symbol, day, interval):
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


def _ingest_one(
    store: ParquetStore, raw_root: Path, sym: str, day: date, args, cfg: EngineConfig,
) -> str | None:
    """Ingest one (symbol, session). Returns a refusal/failure message or None.

    Order matters: the chain is synthesized and VALIDATED before anything is
    written, so a refused session leaves no partial store state (no tape
    without its chain)."""
    part = raw_root / f"ticker={sym}" / f"date={day.isoformat()}"
    missing = [n for n in ("trades.parquet", "quotes.parquet") if not (part / n).exists()]
    if missing:
        return f"raw partition incomplete (missing {', '.join(missing)}) - skipped"
    trades = pd.read_parquet(part / "trades.parquet", engine="pyarrow")
    quotes = pd.read_parquet(part / "quotes.parquet", engine="pyarrow")
    q = PER_SYMBOL_Q.get(sym, 0.0)

    oi_src_day: date | None
    if args.oi_policy == "prev_session":
        oi, oi_src_day = prev_session_oi(raw_root, sym, day)
        oi_label = "prev_session"
    else:
        oi_path = part / "oi.parquet"
        if oi_path.exists():
            oi = pd.read_parquet(oi_path, engine="pyarrow")
            oi_src_day = day
            oi_label = "same_day_eod_LEAKY"
        else:
            # Fall back to the PIT-safe source AND say so — the sidecar must
            # describe what was actually used, not what was asked for.
            oi, oi_src_day = prev_session_oi(raw_root, sym, day)
            oi_label = "prev_session (same_day_eod fallback: oi.parquet absent)"

    try:
        chain, stats = synthesize_chain(
            quotes, oi, symbol=sym, day=day, cadence=args.cadence,
            r=cfg.gate.risk_free_rate, q=q, session=cfg.session,
            latency=pd.Timedelta(milliseconds=cfg.data.chain_latency_ms),
        )
    except ValueError as e:
        return f"REFUSED - {e}"

    same, other = validate_same_day_expiry(chain, day)
    if same == 0 and not args.allow_nonzero_dte:
        return (
            f"REFUSED - chain has 0 same-day-expiry rows ({other} other-tenor); "
            "engine option features would be None all session "
            "(pass --allow-nonzero-dte to override)"
        )

    if oi_src_day is not None:
        gap = max(len(trading_days(oi_src_day, day)) - 1, 0)
        if gap > OI_STALENESS_WARN_SESSIONS:
            stats.warnings.append(
                f"OI source {oi_src_day} is {gap} trading sessions before {day} - "
                "gamma weights are stale"
            )

    # Chain accepted -> now write everything for the session.
    ingest_tape(store, sym, day, trades,
                latency=pd.Timedelta(milliseconds=cfg.data.tape_latency_ms))
    store.write_option_quotes(sym, day, quotes, source=DataSource.THETA,
                              interval=args.quote_interval)
    sidecar = {
        "synthesized": True,
        "method": "bs_mid_inversion+parity_spot",
        "iv_T": "max((expiry - day).days, 1)/365  (consumer clock, R7)",
        "oi_policy": oi_label,
        "oi_source_date": oi_src_day.isoformat() if oi_src_day else None,
        "cadence": args.cadence,
        "r": cfg.gate.risk_free_rate, "q": q,
        **stats.as_dict(),
    }
    store.write_chain(
        OptionChainSeries(sym, chain, DataSource.THETA_DERIVED), day, synthesis=sidecar
    )

    if args.parity_bars:
        ingest_parity_bars(store, sym, day, quotes,
                           interval=args.parity_interval, cfg=cfg, q=q)
    print(f"  {sym} {day}: tape={len(trades)} chain_rows={stats.rows_emitted} "
          f"(dropped_iv={stats.rows_dropped_iv}, no_spot_snaps="
          f"{stats.snapshots_no_spot}/{stats.snapshots_total}, oi={oi_label})")
    return None


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
    problems = 0
    for sym in (s.upper() for s in args.symbols):
        for day in _raw_partitions(args.raw_root, sym, args.start, args.end):
            try:
                msg = _ingest_one(store, args.raw_root, sym, day, args, cfg)
            except Exception as e:  # loud, per-partition, run continues
                msg = f"FAILED - {type(e).__name__}: {e}"
            if msg is not None:
                print(f"  {sym} {day}: {msg}")
                problems += 1
    return 0 if problems == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
