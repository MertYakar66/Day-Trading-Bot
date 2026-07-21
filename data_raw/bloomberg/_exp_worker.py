"""Concurrency experiment worker — pull ONE symbol for ONE day, write isolated JSON.

Reuses pull_spread_stats.py's Acc/from_bbg/from_gz (adaptive window). Writes to its own
<out>.json (never the shared spread_stats.csv) so two of these can run in parallel with no
write race. Logs its own wall-clock so the parallel envelope can be compared to the serial sum.
Usage: python _exp_worker.py SPY 2026-05-21 _exp_SPY.json
"""
import sys
import os
import json
import time
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pss", os.path.join(HERE, "pull_spread_stats.py"))
pss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pss)

sym, dstr, outp = sys.argv[1], sys.argv[2], sys.argv[3]
wmin, avail = pss.pick_window_min()
print(f"[worker {sym}] start {dstr} wmin={wmin} avail={avail:.1f}GB", flush=True)
acc = pss.Acc()
t0 = time.monotonic()
gz = os.path.join(pss.TICKS, f"{sym}_ticks_{dstr}.csv.gz")
try:
    if os.path.exists(gz) and os.path.getsize(gz) > 1024:
        pss.from_gz(gz, acc); src = "gz"
    else:
        pss.from_bbg(sym, dstr, acc, wmin); src = "bbg"
    r = acc.result(sym, dstr)
    err = None
except Exception as e:
    r = None; src = "err"; err = f"{type(e).__name__} {str(e)[:90]}"
wall = time.monotonic() - t0
print(f"[worker {sym}] DONE {dstr} wall={wall:.0f}s src={src} "
      f"peakRSS={pss.proc_peak_gb():.2f}GB err={err}", flush=True)
with open(outp, "w") as f:
    json.dump({"r": r, "wall": round(wall, 1), "sym": sym, "dstr": dstr, "src": src, "err": err}, f)
