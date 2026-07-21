"""Day-bot PULL 2 — intraday 1-minute bars (bdib). Bloomberg lab 2026-06-17.

Per (symbol, trading-day): RTH 1-min bars, accumulated into ONE file per symbol.
bdib returns canonical, UTC, START-labelled bars (verified: open bar = 13:30:00Z EDT).
Explicit headers: ts,open,high,low,close,volume,numEvents (value=$volume kept too).
Output: data_raw/bloomberg/bars_1m/<SYM>_1m.csv (one per symbol, all days).

Universe: 24 ETFs/equities ("<SYM> US Equity") + SPX Index.
Usage:  python pull_bars.py 2026-01-28 2026-06-17  [SYM1,SYM2,...]
Reproducible producer.
"""
import os
import sys
import pandas as pd
from xbbg import blp

OUT = os.path.join(os.path.dirname(__file__), "bars_1m")
COLS = ["ts", "open", "high", "low", "close", "volume", "numEvents", "value"]
DEFAULT = ("SPY QQQ IWM DIA XLK XLF XLE XLV XLY SMH GLD TLT AAPL MSFT NVDA AMZN META "
           "GOOGL TSLA AMD NFLX JPM XOM AVGO SPX").split()


def bbg(sym):
    return "SPX Index" if sym == "SPX" else f"{sym} US Equity"


def native(x):
    return x.to_native() if hasattr(x, "to_native") else x


if __name__ == "__main__":
    start, end = sys.argv[1], sys.argv[2]
    syms = sys.argv[3].split(",") if len(sys.argv) > 3 else DEFAULT
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]
    os.makedirs(OUT, exist_ok=True)
    for sym in syms:
        frames = []
        for dstr in days:
            try:
                d = native(blp.bdib(bbg(sym), dstr, session="allday"))
            except Exception:
                continue
            if not hasattr(d, "columns") or not len(d) or "time" not in d.columns:
                continue
            d = d.rename(columns={"time": "ts"})
            for c in COLS:
                if c not in d.columns:
                    d[c] = pd.NA
            frames.append(d[COLS])
        if not frames:
            print(f"  {sym}: no bars"); continue
        out = pd.concat(frames, ignore_index=True)
        out["ts"] = pd.to_datetime(out["ts"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        fp = os.path.join(OUT, f"{sym}_1m.csv")
        out.to_csv(fp, index=False)
        print(f"  {sym:6}: {len(out):>7} bars {out['ts'].min()}..{out['ts'].max()} {os.path.getsize(fp)//1024}KB")
