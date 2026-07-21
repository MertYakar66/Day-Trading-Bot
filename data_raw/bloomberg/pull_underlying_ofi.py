"""Day-bot STEP 3 — underlying per-minute signed order-flow (OFI) for SPY/QQQ (Bloomberg lab
2026-07-03). Same tick source as spread_stats (underlying bdtick TRADE/BID/ASK), so this is a
near-free intraday-flow feature.

Per (symbol, day): pull RTH ticks in windows (bounded RAM; carry NBBO ffill + Lee-Ready tick-state
across windows), classify each TRADE via Lee-Ready, aggregate per minute:
  buy_vol  = sum size of buy-initiated trades
  sell_vol = sum size of sell-initiated trades
  ofi      = (buy_vol - sell_vol) / (buy_vol + sell_vol)      # 'mid' trades excluded (ambiguous)
Emit/append underlying_ofi/<SYM>_1m.csv (ts, buy_vol, sell_vol, ofi), idempotent on ts, UTC,
start-labelled minutes. NOTE: this is a NEW artifact (the engine currently derives OFI from the
OPTION tape); documented as such. Raw ticks discarded.

Usage: python pull_underlying_ofi.py SPY 2026-07-02   (one symbol-day; appends)
"""
import os
import sys
import time
import numpy as np
import pandas as pd
import option_derive as od
from xbbg import blp

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "underlying_ofi")
COLS = ["ts", "buy_vol", "sell_vol", "ofi"]
WINDOW_MIN = 90


def rth_windows(dstr, wmin):
    from datetime import timedelta
    o, c = od.rth_utc_bounds(dstr)
    a = o
    while a < c:
        b = min(a + timedelta(minutes=wmin), c)
        yield a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d %H:%M:%S")
        a = b


def main():
    sym = sys.argv[1].upper()
    dstr = sys.argv[2]
    os.makedirs(OUT, exist_ok=True)
    fp = os.path.join(OUT, f"{sym}_1m.csv")
    if os.path.exists(fp):
        ex = pd.read_csv(fp)
        if (ex["ts"].astype(str).str[:10] == dstr).any():
            print(f"{sym} {dstr}: already present (skip)", flush=True); return

    last_bid = last_ask = None
    trade_frames = []
    tick_tot = 0
    t0 = time.monotonic()
    for x, y in rth_windows(dstr, WINDOW_MIN):
        d = od.native(blp.bdtick(f"{sym} US Equity", x, y, event_types=["TRADE", "BID", "ASK"]))
        if not (hasattr(d, "columns") and {"time", "type", "value", "size"}.issubset(d.columns) and len(d)):
            continue
        d = d.rename(columns={"time": "ts"})[["ts", "type", "value", "size"]]
        d["ts"] = pd.to_datetime(d["ts"], utc=True)
        d = d.sort_values("ts", kind="stable").reset_index(drop=True)
        tick_tot += len(d)
        bid = d["value"].where(d["type"] == "BID").ffill()
        ask = d["value"].where(d["type"] == "ASK").ffill()
        if last_bid is not None:
            bid = bid.fillna(last_bid)
        if last_ask is not None:
            ask = ask.fillna(last_ask)
        tr = d[d["type"] == "TRADE"]
        if len(tr):
            trade_frames.append(pd.DataFrame({
                "ts": tr["ts"].values, "price": tr["value"].values, "size": tr["size"].values,
                "nbbo_bid": bid[tr.index].values, "nbbo_ask": ask[tr.index].values}))
        if len(bid.dropna()):
            last_bid = bid.iloc[-1]
        if len(ask.dropna()):
            last_ask = ask.iloc[-1]
        del d

    if not trade_frames:
        print(f"{sym} {dstr}: no trades (holiday?)", flush=True); return
    trades = pd.concat(trade_frames, ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)
    trades["side"] = od.lee_ready(trades)
    trades["minute"] = trades["ts"].dt.floor("1min")
    grid = od.rth_minute_grid(dstr)
    buy = trades[trades["side"] == "buy"].groupby("minute")["size"].sum()
    sell = trades[trades["side"] == "sell"].groupby("minute")["size"].sum()
    df = pd.DataFrame(index=grid)
    df["buy_vol"] = buy.reindex(grid).fillna(0).astype("int64")
    df["sell_vol"] = sell.reindex(grid).fillna(0).astype("int64")
    tot = df["buy_vol"] + df["sell_vol"]
    df["ofi"] = np.where(tot > 0, (df["buy_vol"] - df["sell_vol"]) / tot, 0.0).round(6)
    df.index.name = "ts"
    df = df.reset_index()
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    if os.path.exists(fp):
        allrows = pd.concat([pd.read_csv(fp), df], ignore_index=True)
    else:
        allrows = df
    allrows = allrows[COLS].drop_duplicates("ts", keep="last").sort_values("ts")
    tmp = fp + ".tmp"
    allrows.to_csv(tmp, index=False, lineterminator="\n")
    os.replace(tmp, fp)
    wall = time.monotonic() - t0
    net = int(df["buy_vol"].sum() - df["sell_vol"].sum())
    print(f"{sym} {dstr}: {tick_tot:,} ticks -> {len(trades):,} trades, day buy={int(df['buy_vol'].sum()):,} "
          f"sell={int(df['sell_vol'].sum()):,} net={net:+,} | {fp} now {len(allrows)} rows wall={wall:.0f}s", flush=True)


if __name__ == "__main__":
    main()
