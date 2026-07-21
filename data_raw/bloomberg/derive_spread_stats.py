"""Day-bot — derive durable spread-calibration stats from raw NBBO tick gz (Option A).

Raw ticks are perishable + gitignored (data_raw convention); THIS small output is the committed,
durable deliverable that replaces the day-bot's 5 bps fallback spread. Idempotent + resumable:
processes every ticks/<SYM>_ticks_<date>.csv.gz not already in spread_stats.csv, appends, re-sorts.

Per (symbol, date) from the BID/ASK/TRADE stream (cols ts,type,value,size):
  prevailing NBBO via ffill -> quoted spread; effective spread at trades (2*|px-mid|);
  time-weighted quoted spread; sizes; in $ and bps. Garbage quotes (crossed / >5% of mid) dropped.

Usage: python derive_spread_stats.py            (process all new gz)
       python derive_spread_stats.py 2026-06-17 (limit to a date substring)
"""
import os
import sys
import glob
import pandas as pd

HERE = os.path.dirname(__file__)
TICKS = os.path.join(HERE, "ticks")
OUTF = os.path.join(HERE, "spread_stats.csv")


def stats_for(fp):
    sym_date = os.path.basename(fp).replace("_ticks_", "|").replace(".csv.gz", "")
    sym, date = sym_date.split("|")
    df = pd.read_csv(fp)
    if not {"ts", "type", "value", "size"}.issubset(df.columns) or not len(df):
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="mixed")
    df = df.sort_values("ts").reset_index(drop=True)
    df["bid"] = df["value"].where(df["type"] == "BID").ffill()
    df["ask"] = df["value"].where(df["type"] == "ASK").ffill()
    df["spread"] = df["ask"] - df["bid"]
    df["mid"] = (df["ask"] + df["bid"]) / 2
    # valid NBBO: both sides present, not crossed, spread < 5% of mid (drop garbage)
    v = df[(df["bid"] > 0) & (df["ask"] >= df["bid"]) & (df["spread"] < 0.05 * df["mid"])].copy()
    if not len(v):
        return None
    q_bps = (v["spread"] / v["mid"] * 1e4)
    # time-weighted quoted spread over RTH (weight each spread by seconds to next observation)
    dt = v["ts"].diff().dt.total_seconds().shift(-1).clip(lower=0)
    twa = (v["spread"] * dt).sum() / dt.sum() if dt.sum() > 0 else float("nan")
    twa_bps = (twa / v["mid"].median() * 1e4) if v["mid"].median() else float("nan")
    # effective spread at trades
    tr = df[(df["type"] == "TRADE") & df["mid"].notna() & (df["mid"] > 0)].copy()
    eff = 2 * (tr["value"] - tr["mid"]).abs()
    eff_bps = (eff / tr["mid"] * 1e4)
    bsz = df.loc[df["type"] == "BID", "size"]
    asz = df.loc[df["type"] == "ASK", "size"]
    return {
        "symbol": sym, "date": date,
        "n_ticks": len(df), "n_trades": int((df["type"] == "TRADE").sum()),
        "n_quotes": int((df["type"].isin(["BID", "ASK"])).sum()),
        "q_spread_med": round(v["spread"].median(), 5), "q_spread_mean": round(v["spread"].mean(), 5),
        "q_spread_bps_med": round(q_bps.median(), 4), "q_spread_bps_mean": round(q_bps.mean(), 4),
        "twa_spread": round(twa, 5), "twa_spread_bps": round(twa_bps, 4),
        "eff_spread_med": round(eff.median(), 5) if len(eff) else None,
        "eff_spread_bps_med": round(eff_bps.median(), 4) if len(eff) else None,
        "mid_med": round(v["mid"].median(), 4),
        "bid_sz_med": int(bsz.median()) if len(bsz) else None,
        "ask_sz_med": int(asz.median()) if len(asz) else None,
        "first_ts": v["ts"].min().strftime("%H:%M:%SZ"), "last_ts": v["ts"].max().strftime("%H:%M:%SZ"),
    }


def main():
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    done = set()
    if os.path.exists(OUTF):
        ex = pd.read_csv(OUTF)
        done = set(zip(ex["symbol"], ex["date"].astype(str)))
    files = sorted(glob.glob(os.path.join(TICKS, "*_ticks_*.csv.gz")))
    new = []
    for fp in files:
        if flt and flt not in os.path.basename(fp):
            continue
        sd = os.path.basename(fp).replace("_ticks_", "|").replace(".csv.gz", "").split("|")
        if (sd[0], sd[1]) in done:
            continue
        try:
            r = stats_for(fp)
            if r:
                new.append(r); print(f"  {sd[0]} {sd[1]}: q_spread={r['q_spread_bps_med']}bps eff={r['eff_spread_bps_med']}bps twa={r['twa_spread_bps']}bps mid={r['mid_med']} (n={r['n_ticks']})", flush=True)
        except Exception as e:
            print(f"  {os.path.basename(fp)}: ERR {type(e).__name__} {str(e)[:60]}", flush=True)
    if new:
        allrows = (pd.concat([pd.read_csv(OUTF), pd.DataFrame(new)], ignore_index=True)
                   if os.path.exists(OUTF) else pd.DataFrame(new))
        allrows = allrows.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
        allrows.to_csv(OUTF, index=False)
        print(f"spread_stats.csv: +{len(new)} -> {len(allrows)} rows", flush=True)
    else:
        print("no new tick files to process", flush=True)


if __name__ == "__main__":
    main()
