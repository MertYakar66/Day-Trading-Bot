"""S1 gamma-regime pilot — STAGE 1: build a dedicated SPX store (new files only).

Builds C:/Users/merty/Desktop/Day-Trading-Bot/data_raw/swe_tests/store_spx with
  * bars/ticker=SPX/date=<D>/bars_5m.parquet    (DataSource.IBKR provenance)
      ingested from data_raw/ibkr/SPX_5m_3mo.json via the SAME transform the
      operator script uses (intraday.data.ibkr.bars_by_day with the EngineConfig
      defaults), through a copy placed under data_raw/swe_tests/raw_spx/ so the
      original raw dir is never touched.
  * option_chain/ticker=SPX/date=<D>/chain.parquet (DataSource.THETA provenance)
      mapped from the read-only SWE Theta EOD index snapshots
      (SPX_20260524 / SPX_20260601 — single-expiry monthly SPX files; the SPXW
      weeklies are EXCLUDED: their open_interest is mostly zero, see stage-2
      disclosure). POINT-IN-TIME rule: an EOD snapshot is available from the
      NEXT session's open at the earliest, so each day partition holds the most
      recent snapshot whose (conservative) availability precedes that day's
      open, with available_ts stamped at that day's 09:30 ET open.
  * option_tape/ticker=SPX/date=<D>/trades.parquet (DataSource.THETA provenance)
      an EMPTY, correctly-typed tape: no SPX option tape was ever captured.
      This is deliberate and honest — OFI will be None, and stage 2 measures
      what that does to S1.

PIT mapping used (verified in stage 2 output):
  - snapshot 20260423 (underlying_timestamp 2026-04-22T16:05): would trade
    2026-04-23/24 — NO SPX bars exist then (the "3mo" IBKR capture fell back to
    ONE_WEEK at the 1000-bar cap), so it is UNUSABLE for this pilot.
  - snapshot 20260524 (Sunday; no underlying_timestamp column; spot 7473.47 —
    inferred Friday 2026-05-22 close): available 2026-05-26 open; first session
    with bars is 2026-05-27 -> serves 05-27, 05-28, 05-29 and (still latest
    available under the conservative reading) 06-01.
  - snapshot 20260601 (Monday; no underlying_timestamp column; spot 7596.05 is
    closer to the 06-01 close 7600.25 than the 05-29 close 7581.25):
    CONSERVATIVELY treated as 06-01 EOD -> tradable from 06-02 open. 06-02 in
    the capture is PARTIAL (21/78 bars) and is dropped by the ingest coverage
    guard, so this snapshot gets NO Plan-A store partition; stage 2 dry-runs it.

Run:
    cd C:/Users/merty/Desktop/Day-Trading-Bot
    .venv/Scripts/python.exe -m scripts.swe_tests.s1_spx_pilot_build_store
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pandas as pd

from intraday.config import EngineConfig
from intraday.contracts import (
    CHAIN_COLUMNS,
    TAPE_COLUMNS,
    AssetKind,
    DataSource,
    OptionChainSeries,
    OptionTape,
)
from intraday.data.ibkr import bars_by_day
from intraday.data.store import ParquetStore
from intraday.timeutils import session_bounds_utc

BOT = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
RAW_SRC = BOT / "data_raw/ibkr/SPX_5m_3mo.json"
RAW_DIR = BOT / "data_raw/swe_tests/raw_spx"
STORE_ROOT = BOT / "data_raw/swe_tests/store_spx"
CHAIN_DIR = SWE / "data_processed/theta/index_options_chains"

ET = "America/New_York"

# day partition -> (snapshot file, inferred snapshot close ts ET, note)
CHAIN_PLAN: dict[date, tuple[str, str, str]] = {
    date(2026, 5, 27): ("SPX_20260524.parquet", "2026-05-22 16:00", "pilot day: first session with bars after snapshot availability (05-26 has no bars)"),
    date(2026, 5, 28): ("SPX_20260524.parquet", "2026-05-22 16:00", "stale carry-forward (latest available)"),
    date(2026, 5, 29): ("SPX_20260524.parquet", "2026-05-22 16:00", "stale carry-forward (latest available)"),
    date(2026, 6, 1): ("SPX_20260524.parquet", "2026-05-22 16:00", "stale carry-forward; 20260601 snapshot conservatively NOT available intraday 06-01"),
}


def map_theta_chain(
    src: Path, day: date, snapshot_close_et: str
) -> pd.DataFrame:
    """Map one SWE Theta EOD parquet onto the bot's CHAIN_COLUMNS contract.

    right->option_type, iv->implied_vol, underlying_price->spot. Single-expiry
    file (verified) so caveat #1 (multi-expiry blending) cannot apply.
    """
    df = pd.read_parquet(src)
    exps = pd.to_datetime(df["expiration"]).dt.normalize().unique()
    if len(exps) != 1:
        raise SystemExit(f"{src.name}: expected single expiry, got {list(exps)}")
    open_utc, _ = session_bounds_utc(day)
    snap_ts = pd.Timestamp(snapshot_close_et, tz=ET).tz_convert("UTC")
    out = pd.DataFrame(
        {
            "snapshot_ts": snap_ts,
            "available_ts": open_utc,  # PIT: usable from this day's open (>= true availability)
            "expiration": pd.Timestamp(exps[0]),
            "strike": df["strike"].astype(float),
            "option_type": df["right"].astype(str),
            "open_interest": df["open_interest"].astype("int64"),
            "implied_vol": df["iv"].astype(float),
            "spot": df["underlying_price"].astype(float),
        }
    )
    return out[list(CHAIN_COLUMNS)]


def empty_tape_frame() -> pd.DataFrame:
    """An empty option tape with the contract's columns and sane dtypes."""
    f = pd.DataFrame(columns=list(TAPE_COLUMNS))
    f["ts"] = pd.to_datetime(f["ts"], utc=True)
    f["available_ts"] = pd.to_datetime(f["available_ts"], utc=True)
    for col in ("strike", "price", "size", "nbbo_bid", "nbbo_ask"):
        f[col] = f[col].astype(float)
    return f


def main() -> int:
    cfg = EngineConfig.default()
    store = ParquetStore(STORE_ROOT)

    # 1) Bars: copy raw payload into our sandbox dir, ingest with the operator
    #    script's exact transform + defaults (min_coverage=0.8 drops the partial
    #    06-02 session rather than padding a fabricated flat afternoon).
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_copy = RAW_DIR / "SPX_5m_3mo.json"
    if not raw_copy.exists():
        shutil.copyfile(RAW_SRC, raw_copy)
    payload = json.loads(raw_copy.read_text())
    by_day = bars_by_day(
        "SPX",
        payload,
        "5m",
        session=cfg.session,
        latency=pd.Timedelta(milliseconds=cfg.data.bar_latency_ms),
        asset_kind=AssetKind.INDEX,
        min_coverage=0.8,
    )
    for day, bars in by_day.items():
        store.write_bars(bars, day)
    kept = sorted(by_day)
    print(f"bars: {len(payload['time'])} raw 5m bars -> {len(kept)} sessions kept: {kept}")
    print(f"  fetch note (raw payload): {payload.get('_fetch_note')}")

    # 2) Chains + empty tapes per stored session.
    report = {}
    for day, (fname, snap_et, note) in CHAIN_PLAN.items():
        if day not in by_day:
            print(f"chain: skip {day} (no bars kept)")
            continue
        frame = map_theta_chain(CHAIN_DIR / fname, day, snap_et)
        store.write_chain(OptionChainSeries("SPX", frame, DataSource.THETA), day)
        store.write_tape(OptionTape("SPX", empty_tape_frame(), DataSource.THETA), day)
        oi = frame["open_interest"]
        report[str(day)] = {
            "snapshot_file": fname,
            "snapshot_close_et_inferred": snap_et,
            "available_ts_utc": str(frame["available_ts"].iloc[0]),
            "expiration": str(frame["expiration"].iloc[0].date()),
            "rows": int(len(frame)),
            "oi_zero_frac": round(float((oi == 0).mean()), 4),
            "oi_median": float(oi.median()),
            "spot": float(frame["spot"].iloc[0]),
            "note": note,
        }
        print(f"chain: {day} <- {fname} ({len(frame)} rows, exp {report[str(day)]['expiration']}) | empty tape written")

    out = BOT / "data_raw/swe_tests/s1_pilot"
    out.mkdir(parents=True, exist_ok=True)
    (out / "store_build_report.json").write_text(json.dumps(report, indent=2))
    print(f"store built at {STORE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
