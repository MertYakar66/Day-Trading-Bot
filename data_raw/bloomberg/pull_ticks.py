"""Day-bot PULL 1 — intraday NBBO + trades (bdtick). Bloomberg lab 2026-06-17.

Per (symbol, trading-day): RTH ticks (TRADE/BID/ASK). RTH = 09:30-16:00 America/New_York
converted to UTC per date (handles the EST<->EDT boundary). bdtick query datetimes are UTC and
the returned `time` is already correct UTC (verified: open burst lands at 13:30Z EDT) — stored
as-is, no conversion. Canonical, explicit headers: ts,type,value,size (ts = ISO UTC). One gzipped
file per (symbol, date): data_raw/bloomberg/ticks/<SYM>_ticks_<YYYY-MM-DD>.csv.gz.

Usage:  python pull_ticks.py SPY,QQQ 2026-06-10 2026-06-16
Reproducible producer (the engine had connector files with no producer — not repeating that).
"""
import os
import sys
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from xbbg import blp

OUT = os.path.join(os.path.dirname(__file__), "ticks")
ET, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")
TYPES = ["TRADE", "BID", "ASK"]


def rth_utc(dstr):
    y, m, d = map(int, dstr.split("-"))
    a = datetime(y, m, d, 9, 30, tzinfo=ET).astimezone(UTC)
    b = datetime(y, m, d, 16, 0, tzinfo=ET).astimezone(UTC)
    return a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d %H:%M:%S")


def native(x):
    return x.to_native() if hasattr(x, "to_native") else x


if __name__ == "__main__":
    syms = sys.argv[1].split(",")
    # RECENT-FIRST: the pull is slow (~12 min/symbol-day) and may not finish in one session.
    # The day-bot needs recent days most (current-spread calibration); the oldest (lowest-value)
    # days backfill last and can be extended by a future re-run. Resumable, so order is free to change.
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(sys.argv[2], sys.argv[3])][::-1]
    os.makedirs(OUT, exist_ok=True)
    for dstr in days:
        a, b = rth_utc(dstr)
        for sym in syms:
            fp = os.path.join(OUT, f"{sym}_ticks_{dstr}.csv.gz")
            if os.path.exists(fp) and os.path.getsize(fp) > 1024:   # resumable: skip completed
                print(f"  {sym} {dstr}: skip (exists)", flush=True)
                continue
            try:
                d = native(blp.bdtick(f"{sym} US Equity", a, b, event_types=TYPES))
            except Exception as e:
                print(f"  {sym} {dstr}: ERR {type(e).__name__} {str(e)[:50]}", flush=True)
                continue
            if not hasattr(d, "columns") or not {"time", "type", "value", "size"}.issubset(d.columns) or not len(d):
                print(f"  {sym} {dstr}: empty (holiday?)", flush=True)
                continue
            out = d.rename(columns={"time": "ts"})[["ts", "type", "value", "size"]].copy()
            out["ts"] = pd.to_datetime(out["ts"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
            tmp = fp + ".tmp"                                       # atomic: write tmp then rename
            out.to_csv(tmp, index=False, compression="gzip")
            os.replace(tmp, fp)
            vc = d["type"].value_counts().to_dict()
            print(f"  {sym} {dstr}: {len(out):>8} ticks (T={vc.get('TRADE',0)} B={vc.get('BID',0)} A={vc.get('ASK',0)}) "
                  f"{os.path.getsize(fp)//1024}KB  {out['ts'].iloc[0]}..{out['ts'].iloc[-1]}", flush=True)
