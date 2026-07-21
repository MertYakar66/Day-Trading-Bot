"""Day-bot intraday OPTION pull+derive (Bloomberg lab 2026-07-03). SPY / SPX(SPXW) / QQQ.

DECOUPLED widths (the key design): CHAIN is wide+cheap (bdib IV), TAPE is narrow+expensive (bdtick):
  CHAIN  ATM +- chain_radius (10), expiries [0DTE, next weekly]  -> bdib BID/ASK bars -> per-min
         mid -> EUROPEAN Black-Scholes IV (r=0.04, q=0, PIT spot); + bdh OPEN_INT (daily).
  TAPE   ATM +- tape_radius (3), expiry [0DTE]                   -> bdtick TRADE/BID/ASK -> NBBO
         from the last quote STRICTLY BEFORE each trade -> Lee-Ready side_inferred {buy,sell,mid}.

Emits engine-canonical, gzipped, one per (symbol,day):
  option_chain/<SYM>_<D>.csv.gz : snapshot_ts,available_ts,expiration,strike,option_type,
                                  open_interest,implied_vol,spot  (+ bonus bid,ask,mid)  [1-min]
  option_tape/<SYM>_<D>.csv.gz  : ts,available_ts,expiration,strike,right,price,size,nbbo_bid,
                                  nbbo_ask,side_inferred
available_ts = event ts (arrival latency added at ingest). Raw ticks discarded after folding.
PIT spot = close of the bar ENDING at t (bars_1m start-labelled -> shift). Per-contract retry
(1-2x backoff) then skip+log. Reads only; UTC; canonical headers; atomic (.tmp->rename); resumable.

Usage: python pull_options.py SPY 2026-07-02 [chain_radius=10] [tape_radius=3]
"""
import os
import sys
import time
import gzip
import numpy as np
import pandas as pd
import option_derive as od
from xbbg import blp

HERE = os.path.dirname(os.path.abspath(__file__))
BARS = os.path.join(HERE, "bars_1m")
CHAIN_DIR = os.path.join(HERE, "option_chain")
TAPE_DIR = os.path.join(HERE, "option_tape")
TAPE_COLS = ["ts", "available_ts", "expiration", "strike", "right", "price", "size",
             "nbbo_bid", "nbbo_ask", "side_inferred"]
CHAIN_COLS = ["snapshot_ts", "available_ts", "expiration", "strike", "option_type",
              "open_interest", "implied_vol", "spot", "bid", "ask", "mid"]
HOLIDAYS = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
            "2026-07-03", "2026-09-07", "2026-11-26", "2026-11-27", "2026-12-24", "2026-12-25"}


def log(m):
    print(m, flush=True)


def with_retry(fn, tries=3, base=2.0):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(base * (i + 1))


def next_weekly(dstr):
    """Next standard weekly expiry strictly after dstr: the next Friday, skipping FORWARD a week if
    that Friday is a holiday (that week's weekly then settled on the prior 0DTE, so the *next* weekly
    is the following Friday — never fold back onto dstr itself)."""
    d = pd.Timestamp(dstr)
    delta = (4 - d.weekday()) % 7 or 7
    fri = d + pd.Timedelta(days=delta)
    while fri.strftime("%Y-%m-%d") in HOLIDAYS:
        fri += pd.Timedelta(days=7)
    return fri.strftime("%Y-%m-%d")


def load_bars_day(sym, dstr):
    fp = os.path.join(BARS, f"{od.SYMCFG[sym]['bars']}_1m.csv")
    if not os.path.exists(fp):
        return None
    b = pd.read_csv(fp)
    b["ts"] = pd.to_datetime(b["ts"], utc=True)
    b = b[b["ts"].dt.strftime("%Y-%m-%d") == dstr].sort_values("ts").set_index("ts")
    return b if len(b) else None


def resolve_open_int(tickers, dstr):
    oi = {t: None for t in tickers}
    start = (pd.Timestamp(dstr) - pd.Timedelta(days=8)).strftime("%Y-%m-%d")
    try:
        d = od.native(blp.bdh(tickers, "OPEN_INT", start, dstr))
        if hasattr(d, "columns") and len(d) and {"ticker", "date", "value"}.issubset(set(map(str, d.columns))):
            d = d[d["date"].astype(str) <= dstr].sort_values("date")
            for t, g in d.groupby("ticker"):
                v = g["value"].dropna()
                if len(v):
                    oi[str(t)] = int(v.iloc[-1])
    except Exception as e:
        log(f"  [oi] bdh OPEN_INT failed: {type(e).__name__} {str(e)[:60]}")
    return oi


def pull_tape_contract(tk, o_utc, c_utc):
    x, y = o_utc.strftime("%Y-%m-%d %H:%M:%S"), c_utc.strftime("%Y-%m-%d %H:%M:%S")
    d = od.native(blp.bdtick(tk, x, y, event_types=["TRADE", "BID", "ASK"]))
    if not (hasattr(d, "columns") and {"time", "type", "value", "size"}.issubset(d.columns) and len(d)):
        return None, 0
    n = len(d)
    d = d.rename(columns={"time": "ts"})[["ts", "type", "value", "size"]]
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    return od.trade_nbbo(d), n


def pull_chain_contract(tk, dstr, grid):
    bidb = od.native(blp.bdib(tk, dstr, session="allday", typ="BID"))
    askb = od.native(blp.bdib(tk, dstr, session="allday", typ="ASK"))
    return od.bar_close_series(bidb, dstr, grid), od.bar_close_series(askb, dstr, grid)


def main():
    sym = sys.argv[1].upper()
    dstr = sys.argv[2]
    chain_radius = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    tape_radius = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    if sym not in od.SYMCFG or dstr in HOLIDAYS or pd.Timestamp(dstr).weekday() >= 5:
        log(f"{sym} {dstr}: not applicable/trading day, skip"); return
    os.makedirs(CHAIN_DIR, exist_ok=True); os.makedirs(TAPE_DIR, exist_ok=True)
    tape_fp = os.path.join(TAPE_DIR, f"{sym}_{dstr}.csv.gz")
    chain_fp = os.path.join(CHAIN_DIR, f"{sym}_{dstr}.csv.gz")
    if os.path.exists(tape_fp) and os.path.exists(chain_fp) and os.path.getsize(tape_fp) > 30 and os.path.getsize(chain_fp) > 30:
        log(f"{sym} {dstr}: already done (skip)"); return

    cfg = od.SYMCFG[sym]
    bars = load_bars_day(sym, dstr)
    if bars is None:
        log(f"{sym} {dstr}: no underlying bars -> skip"); return
    day_open = float(bars["open"].iloc[0])
    atm = od.atm_strike(day_open, cfg["inc"])
    grid = od.rth_minute_grid(dstr)
    spot = od.pit_spot_series(bars, dstr)
    o_utc, c_utc = od.rth_utc_bounds(dstr)
    nx = next_weekly(dstr)
    chain_expiries = [dstr] if dstr == nx else [dstr, nx]
    tape_expiries = [dstr]                                  # 0DTE only (priority)
    cstrikes = [atm + k * cfg["inc"] for k in range(-chain_radius, chain_radius + 1)]
    tstrikes = [atm + k * cfg["inc"] for k in range(-tape_radius, tape_radius + 1)]
    log(f"{sym} {dstr}: open={day_open:.2f} ATM={atm} inc={cfg['inc']} | chain {len(cstrikes)}x2x{len(chain_expiries)} "
        f"exp={chain_expiries} | tape {len(tstrikes)}x2x1 exp={tape_expiries}")

    # ---- OPEN_INT for all chain contracts ----
    chain_contracts = [(e, k, r) for e in chain_expiries for k in cstrikes for r in ("C", "P")]
    ctks = [od.opt_ticker(cfg, e, k, r) for (e, k, r) in chain_contracts]
    oi_map = resolve_open_int(ctks, dstr)

    # ---- TAPE (bdtick, ATM+-tape_radius, 0DTE) ----
    tape_rows, n_tape_ok, n_tape_skip, tick_tot = [], 0, 0, 0
    t0 = time.monotonic()
    for e in tape_expiries:
        for k in tstrikes:
            for r in ("C", "P"):
                tk = od.opt_ticker(cfg, e, k, r)
                try:
                    tn, n = with_retry(lambda: pull_tape_contract(tk, o_utc, c_utc))
                except Exception as ex:
                    log(f"  [tape skip] {tk}: {type(ex).__name__} {str(ex)[:50]}"); n_tape_skip += 1; continue
                tick_tot += n
                if tn is None or not len(tn):
                    continue
                n_tape_ok += 1
                tn = tn.sort_values("ts", kind="stable").reset_index(drop=True)
                side = od.lee_ready(tn)
                for ts_, px, sz, nb, na, sd in zip(tn["ts"], tn["price"], tn["size"],
                                                   tn["nbbo_bid"], tn["nbbo_ask"], side):
                    tape_rows.append((ts_, ts_, e, float(k), r, float(px), int(sz),
                                      (None if pd.isna(nb) else float(nb)),
                                      (None if pd.isna(na) else float(na)), sd))
    t_tape = time.monotonic() - t0

    # ---- CHAIN (bdib BID/ASK, ATM+-chain_radius, 0DTE+next weekly) ----
    chain_rows, n_chain_ok, n_chain_skip = [], 0, 0
    t1 = time.monotonic()
    for (e, k, r), tk in zip(chain_contracts, ctks):
        try:
            bid_s, ask_s = with_retry(lambda: pull_chain_contract(tk, dstr, grid))
        except Exception as ex:
            log(f"  [chain skip] {tk}: {type(ex).__name__} {str(ex)[:50]}"); n_chain_skip += 1; continue
        oi = oi_map.get(tk)
        got = False
        for mts in grid:
            bid, ask = bid_s.get(mts), ask_s.get(mts)
            if pd.isna(bid) or pd.isna(ask) or bid <= 0 or ask < bid:
                continue
            mid = (float(bid) + float(ask)) / 2.0
            sp = spot.get(mts)
            sp = float(sp) if sp is not None and pd.notna(sp) else float("nan")
            T = od.years_to_expiry(mts, e)
            iv = od.implied_vol(mid, sp, float(k), T, r) if (mid > 0 and np.isfinite(sp)) else float("nan")
            chain_rows.append((mts, mts, e, float(k), r, oi,
                               (None if not np.isfinite(iv) else round(iv, 6)),
                               (None if not np.isfinite(sp) else round(sp, 4)),
                               round(float(bid), 4), round(float(ask), 4), round(mid, 5)))
            got = True
        if got:
            n_chain_ok += 1
    t_chain = time.monotonic() - t1
    log(f"  tape: {n_tape_ok} ok/{n_tape_skip} skip, {tick_tot:,} ticks->{len(tape_rows):,} trades ({t_tape:.0f}s) | "
        f"chain: {n_chain_ok} ok/{n_chain_skip} skip, {len(chain_rows):,} snaps ({t_chain:.0f}s)")

    # ---- write (atomic, gzip, canonical) ----
    tape = pd.DataFrame(tape_rows, columns=TAPE_COLS)
    if len(tape):
        tape = tape.sort_values(["ts", "expiration", "strike", "right"]).reset_index(drop=True)
        for c in ("ts", "available_ts"):
            tape[c] = pd.to_datetime(tape[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    chain = pd.DataFrame(chain_rows, columns=CHAIN_COLS)
    if len(chain):
        chain = chain.sort_values(["snapshot_ts", "expiration", "strike", "option_type"]).reset_index(drop=True)
        for c in ("snapshot_ts", "available_ts"):
            chain[c] = pd.to_datetime(chain[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    for df, fp in ((tape, tape_fp), (chain, chain_fp)):
        tmp = fp + ".tmp"
        with gzip.open(tmp, "wt", newline="", encoding="utf-8") as f:
            df.to_csv(f, index=False)
        os.replace(tmp, fp)
    log(f"  WROTE tape={len(tape):,} rows, chain={len(chain):,} rows -> {os.path.basename(tape_fp)}")


if __name__ == "__main__":
    main()
