"""Day-bot PULL 1 (memory-safe) — streaming spread-calibration stats. Bloomberg lab 2026-06-18.

This 16 GB lab box OOMs on a full-day bdtick (~8-11M ticks -> ~9 GB in RAM). So per (symbol, day):
pull RTH in windows and fold spread stats incrementally (ffill NBBO state carried across windows;
only the small spread/eff float arrays are accumulated) — peak RAM stays low. If a raw gz already
exists on disk (the carried sample + 06-09), derive from it the same chunked way instead of
re-pulling. New days are STATS-ONLY (Option A: raw disposable, never the ~6 GB in git).

Speed: the real bottleneck is Bloomberg per-request latency (~50-100s/window, ~30s fixed), NOT RAM.
So WINDOW_MIN is chosen ADAPTIVELY from free RAM at start — bigger windows = fewer requests = less
fixed-latency overhead, paid for with otherwise-idle RAM. Under memory pressure it shrinks
automatically (and a per-window guard splits a window if free RAM dips below the floor mid-day), so
the autonomous run can never OOM. Per-window pull/proc timing + peak RSS are logged so the ceiling
is observable. Single Bloomberg request stream (no concurrency).

Output: appends to spread_stats.csv (idempotent: skips (symbol,date) already present).
Usage: python pull_spread_stats.py SPY,QQQ 2026-06-08   (one day, both symbols)
"""
import os
import sys
import time
import ctypes
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from xbbg import blp

HERE = os.path.dirname(__file__)
TICKS = os.path.join(HERE, "ticks")
OUTF = os.path.join(HERE, "spread_stats.csv")
ET, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")
TYPES = ["TRADE", "BID", "ASK"]

# --- adaptive window sizing (speed) + hard memory floor (safety) ---
MEM_FLOOR_GB = 2.5   # if free RAM dips below this before a window, split that window in half
WINDOW_CAP = 90      # ceiling on the adaptive window. Lowered 120->90 for the heavy Q1 days (4.5M+ tick
                     # open windows) so 4 concurrent streams keep a comfortable RAM margin on the 16 GB box
COLS = ["symbol", "date", "n_ticks", "n_trades", "n_quotes", "q_spread_med", "q_spread_mean",
        "q_spread_bps_med", "q_spread_bps_mean", "twa_spread", "twa_spread_bps",
        "eff_spread_med", "eff_spread_bps_med", "mid_med", "bid_sz_med", "ask_sz_med",
        "first_ts", "last_ts"]


class _MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def mem_avail_gb():
    try:
        m = _MEMSTAT(); m.dwLength = ctypes.sizeof(_MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1e9
    except Exception:
        return float("inf")


def proc_peak_gb():
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p          # 64-bit pseudo-handle, not truncated int
        gpmi = ctypes.windll.psapi.GetProcessMemoryInfo
        gpmi.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
        c = _PMC(); c.cb = ctypes.sizeof(_PMC)
        if gpmi(k.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return c.PeakWorkingSetSize / 1e9
        return 0.0
    except Exception:
        return 0.0


def pick_window_min():
    """Bigger window when RAM is plentiful (fewer slow Bloomberg requests); shrink under pressure."""
    a = mem_avail_gb()
    w = WINDOW_CAP if a > 5.0 else 90 if a > 3.5 else 60
    return w, a


def native(x):
    return x.to_native() if hasattr(x, "to_native") else x


class Acc:
    """Streaming NBBO spread accumulator; ffill state carried across chunks."""
    def __init__(self):
        self.last_bid = self.last_ask = None
        self.sp = []; self.mids = []; self.eff = []; self.eff_mid = []
        self.bsz = []; self.asz = []
        self.n_ticks = self.n_trades = self.n_quotes = 0
        self.twa_num = 0.0; self.twa_den = 0.0
        self.first_ts = None; self.last_ts = None

    def add(self, df):
        if not len(df):
            return
        df = df.sort_values("ts")
        self.n_ticks += len(df)
        self.n_quotes += int(df["type"].isin(["BID", "ASK"]).sum())
        self.n_trades += int((df["type"] == "TRADE").sum())
        bid = df["value"].where(df["type"] == "BID").ffill()
        ask = df["value"].where(df["type"] == "ASK").ffill()
        if self.last_bid is not None:
            bid = bid.fillna(self.last_bid)
        if self.last_ask is not None:
            ask = ask.fillna(self.last_ask)
        spread = ask - bid
        mid = (ask + bid) / 2
        ok = (bid > 0) & (ask >= bid) & (spread < 0.05 * mid)
        v = pd.DataFrame({"ts": df["ts"].values, "spread": spread.values, "mid": mid.values,
                          "type": df["type"].values, "val": df["value"].values, "size": df["size"].values})[ok.values]
        if len(v):
            self.sp.append(v["spread"].to_numpy()); self.mids.append(v["mid"].to_numpy())
            dt = v["ts"].diff().dt.total_seconds().shift(-1).clip(lower=0).fillna(0)
            self.twa_num += float((v["spread"] * dt).sum()); self.twa_den += float(dt.sum())
            tr = v[v["type"] == "TRADE"]
            if len(tr):
                self.eff.append((2 * (tr["val"] - tr["mid"]).abs()).to_numpy()); self.eff_mid.append(tr["mid"].to_numpy())
            self.bsz.append(df.loc[df["type"] == "BID", "size"].to_numpy())
            self.asz.append(df.loc[df["type"] == "ASK", "size"].to_numpy())
            t0, t1 = v["ts"].min(), v["ts"].max()
            self.first_ts = t0 if self.first_ts is None else min(self.first_ts, t0)
            self.last_ts = t1 if self.last_ts is None else max(self.last_ts, t1)
        self.last_bid = bid.iloc[-1]; self.last_ask = ask.iloc[-1]

    def result(self, sym, date):
        import numpy as np
        if not self.sp:
            return None
        sp = np.concatenate(self.sp); mids = np.concatenate(self.mids)
        q_bps = sp / mids * 1e4
        eff = np.concatenate(self.eff) if self.eff else np.array([])
        effm = np.concatenate(self.eff_mid) if self.eff_mid else np.array([])
        eff_bps = (eff / effm * 1e4) if len(eff) else np.array([])
        bsz = np.concatenate(self.bsz) if self.bsz else np.array([0])
        asz = np.concatenate(self.asz) if self.asz else np.array([0])
        twa = self.twa_num / self.twa_den if self.twa_den else float("nan")
        mid_med = float(np.median(mids))
        return {
            "symbol": sym, "date": date, "n_ticks": self.n_ticks, "n_trades": self.n_trades,
            "n_quotes": self.n_quotes, "q_spread_med": round(float(np.median(sp)), 5),
            "q_spread_mean": round(float(np.mean(sp)), 5), "q_spread_bps_med": round(float(np.median(q_bps)), 4),
            "q_spread_bps_mean": round(float(np.mean(q_bps)), 4), "twa_spread": round(twa, 5),
            "twa_spread_bps": round(twa / mid_med * 1e4, 4) if mid_med else None,
            "eff_spread_med": round(float(np.median(eff)), 5) if len(eff) else None,
            "eff_spread_bps_med": round(float(np.median(eff_bps)), 4) if len(eff_bps) else None,
            "mid_med": round(mid_med, 4), "bid_sz_med": int(np.median(bsz)), "ask_sz_med": int(np.median(asz)),
            "first_ts": self.first_ts.strftime("%H:%M:%SZ"), "last_ts": self.last_ts.strftime("%H:%M:%SZ"),
        }


def rth_windows(dstr, window_min):
    y, m, d = map(int, dstr.split("-"))
    a = datetime(y, m, d, 9, 30, tzinfo=ET).astimezone(UTC)
    close = datetime(y, m, d, 16, 0, tzinfo=ET).astimezone(UTC)
    while a < close:
        b = min(a + timedelta(minutes=window_min), close)
        yield a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d %H:%M:%S")
        a = b


def _mid(a, b):
    fa = datetime.strptime(a, "%Y-%m-%d %H:%M:%S"); fb = datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
    return (fa + (fb - fa) / 2).strftime("%Y-%m-%d %H:%M:%S")


def from_gz(fp, acc):
    for ch in pd.read_csv(fp, chunksize=2_000_000):
        ch["ts"] = pd.to_datetime(ch["ts"], utc=True, format="mixed")
        acc.add(ch)
        del ch


def _pull_one(sym, x, y, acc):
    """One bdtick request [x,y); returns (n_ticks, pull_s, proc_s)."""
    t0 = time.monotonic()
    d = native(blp.bdtick(f"{sym} US Equity", x, y, event_types=TYPES))
    pull_s = time.monotonic() - t0
    n, proc_s = 0, 0.0
    if hasattr(d, "columns") and {"time", "type", "value", "size"}.issubset(d.columns) and len(d):
        d = d.rename(columns={"time": "ts"})[["ts", "type", "value", "size"]]
        d["ts"] = pd.to_datetime(d["ts"], utc=True)
        n = len(d)
        t1 = time.monotonic(); acc.add(d); proc_s = time.monotonic() - t1
        del d
    return n, pull_s, proc_s


def from_bbg(sym, dstr, acc, window_min):
    for a, b in rth_windows(dstr, window_min):
        subs = [(a, b)]
        if mem_avail_gb() < MEM_FLOOR_GB:   # safety: bound peak when RAM is tight
            mid = _mid(a, b); subs = [(a, mid), (mid, b)]
            print(f"  [mem-guard] avail<{MEM_FLOOR_GB}GB -> split {a[11:16]}-{b[11:16]}", flush=True)
        for x, y in subs:
            n, pull_s, proc_s = _pull_one(sym, x, y, acc)
            print(f"  [win {x[11:16]}-{y[11:16]}] {sym} pull={pull_s:.1f}s proc={proc_s:.1f}s "
                  f"n={n} avail={mem_avail_gb():.1f}GB", flush=True)


def main():
    syms = sys.argv[1].split(",")
    dstr = sys.argv[2]
    window_min, avail0 = pick_window_min()
    print(f"[cfg] {dstr} avail={avail0:.1f}GB -> WINDOW_MIN={window_min} (cap={WINDOW_CAP})", flush=True)
    done = set()
    if os.path.exists(OUTF):
        ex = pd.read_csv(OUTF)
        done = set(zip(ex["symbol"], ex["date"].astype(str)))
    new = []
    for sym in syms:
        if (sym, dstr) in done:
            print(f"  {sym} {dstr}: skip (in stats)", flush=True); continue
        acc = Acc()
        gz = os.path.join(TICKS, f"{sym}_ticks_{dstr}.csv.gz")
        t_sym = time.monotonic()
        try:
            if os.path.exists(gz) and os.path.getsize(gz) > 1024:
                from_gz(gz, acc); src = "gz"
            else:
                from_bbg(sym, dstr, acc, window_min); src = "bbg"
            r = acc.result(sym, dstr)
        except Exception as e:
            print(f"  {sym} {dstr}: ERR {type(e).__name__} {str(e)[:70]}", flush=True); continue
        wall = time.monotonic() - t_sym
        if r:
            new.append(r)
            print(f"  {sym} {dstr} [{src}]: q={r['q_spread_bps_med']}bps eff={r['eff_spread_bps_med']}bps "
                  f"twa={r['twa_spread_bps']}bps mid={r['mid_med']} n={r['n_ticks']} "
                  f"wall={wall:.0f}s peakRSS={proc_peak_gb():.2f}GB", flush=True)
        else:
            print(f"  {sym} {dstr}: empty (holiday?)", flush=True)
    if new:
        allrows = (pd.concat([pd.read_csv(OUTF), pd.DataFrame(new)], ignore_index=True)
                   if os.path.exists(OUTF) else pd.DataFrame(new))
        for c in COLS:
            if c not in allrows.columns:
                allrows[c] = pd.NA
        allrows = allrows[COLS].sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
        allrows.to_csv(OUTF, index=False)
        print(f"spread_stats.csv: +{len(new)} -> {len(allrows)} rows  (peakRSS={proc_peak_gb():.2f}GB)", flush=True)


if __name__ == "__main__":
    main()
