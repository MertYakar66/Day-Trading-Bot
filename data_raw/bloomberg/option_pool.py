"""Day-bot intraday OPTION backfill pool — K concurrent (symbol,day) workers, recent-first,
per-symbol-day commit+push, live STATUS.md. Priority SPY -> SPX -> QQQ within each recent day
(S&P spine before the QQQ tail). Each worker writes its OWN gz files (no shared-file race), so
concurrency is clean. Memory admission caps launches under RAM pressure. Resumable (pull_options
skips done days). Autonomous: never blocks; one bad worker is retried by pull_options then skipped.

Usage: python option_pool.py [K=4] [start=2026-02-13] [chain_radius=10] [tape_radius=3]
"""
import os
import sys
import time
import subprocess
import importlib.util
from collections import deque
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
BR = "claude/bloomberg-intraday-pull"
PY = sys.executable
spec = importlib.util.spec_from_file_location("pss", os.path.join(HERE, "pull_spread_stats.py"))
pss = importlib.util.module_from_spec(spec); spec.loader.exec_module(pss)
mem_avail_gb = pss.mem_avail_gb

SYMS = ["SPY", "SPX", "QQQ"]                    # priority order within each day
HOLIDAYS = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
            "2026-07-03", "2026-09-07", "2026-11-26", "2026-11-27", "2026-12-24", "2026-12-25"}
LAST_TD = "2026-07-02"
MIN_LAUNCH_AVAIL = 2.0
WORKER_TIMEOUT = 3600
STATUS = os.path.join(HERE, "STATUS.md")


def log(m): print(f"[opt-pool] {m}", flush=True)


def git(*a, timeout=150):
    try:
        subprocess.run(["git", *a], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}, timeout=timeout)
    except Exception as e:
        log(f"WARN git {type(e).__name__}")


def trading_days(start):
    bd = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, LAST_TD)]
    return [d for d in bd if d not in HOLIDAYS]


def write_status(done, total, last, t0, skips, running):
    pct = 100.0 * done / total if total else 0
    rate = (time.time() - t0) / done if done else 0
    eta_h = rate * (total - done) / 3600 if done else float("nan")
    with open(STATUS, "w", encoding="utf-8") as f:
        f.write(f"# Option backfill — STATUS (autonomous run)\n\n")
        f.write(f"**{done}/{total} symbol-days done ({pct:.1f}%)** | last committed: `{last}` | "
                f"in-flight: {running} | ~{rate:.0f}s/symbol-day | ETA remaining ~{eta_h:.1f}h "
                f"(recent-first: newest days are banked first).\n\n")
        f.write(f"- Scope: SPY/SPX/QQQ, CHAIN ATM±10 (0DTE+next weekly, bdib IV), "
                f"TAPE ATM±3 (0DTE, bdtick Lee-Ready). Window recent-first back to ~today−140.\n")
        f.write(f"- Deliverables: `option_chain/<SYM>_<date>.csv.gz`, `option_tape/<SYM>_<date>.csv.gz` "
                f"(committed per symbol-day). Raw ticks discarded.\n")
        f.write(f"- Persistence: per-symbol-day commit+push; 10-min flush watchdog keeps local==origin.\n")
        if skips:
            f.write(f"- Skips/empties ({len(skips)}): " + ", ".join(skips[-12:]) + "\n")
        f.write(f"\n_Updated { 'mid-run' } — see OPTION_PULL_MANIFEST.md for the full coverage table & caveats._\n")


def commit_day(sym, day):
    tape = f"data_raw/bloomberg/option_tape/{sym}_{day}.csv.gz"
    chain = f"data_raw/bloomberg/option_chain/{sym}_{day}.csv.gz"
    ok = os.path.exists(os.path.join(REPO, tape)) and os.path.exists(os.path.join(REPO, chain))
    if not ok:
        return False
    git("add", "-f", tape, chain, "data_raw/bloomberg/STATUS.md")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO).returncode != 0:
        git("commit", "-q", "-m", f"data(daybot): option tape+chain {sym} {day} [pool]")
        git("push", "origin", BR)
        log(f"committed+pushed {sym} {day}")
    return True


def main():
    K = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    K = max(K, 6)   # floor 6: option workers are light (~0.2-0.3GB peak), so RAM isn't the limit;
                    # Bloomberg serves what it can and QUEUES the rest (non-breaking). Memory
                    # admission (MIN_LAUNCH_AVAIL) still self-throttles if free RAM ever tightens.
    start = sys.argv[2] if len(sys.argv) > 2 else "2026-02-13"
    cr = sys.argv[3] if len(sys.argv) > 3 else "10"
    tr = sys.argv[4] if len(sys.argv) > 4 else "3"
    days = trading_days(start)[::-1]                        # recent-first
    jobs = deque((s, d) for d in days for s in SYMS)       # recent day -> SPY,SPX,QQQ
    total = len(jobs)
    log(f"start K={K} avail={mem_avail_gb():.1f}GB | {total} symbol-days over {len(days)} days "
        f"({days[-1]}..{days[0]}) chain_r={cr} tape_r={tr}")
    running, t0, done, skips = {}, time.time(), 0, []
    write_status(0, total, "-", t0, skips, 0)
    while jobs or running:
        while jobs and len(running) < K and (not running or mem_avail_gb() > MIN_LAUNCH_AVAIL):
            sym, day = jobs.popleft()
            lg = open(os.path.join(HERE, f"_optlog_{sym}_{day}.log"), "w")
            p = subprocess.Popen([PY, os.path.join(HERE, "pull_options.py"), sym, day, cr, tr],
                                 cwd=HERE, stdout=lg, stderr=subprocess.STDOUT)
            running[p] = (sym, day, lg, time.time())
            log(f"launch {sym} {day} (running={len(running)}/{K} avail={mem_avail_gb():.1f}GB, {len(jobs)} queued)")
        fin = [p for p in running if p.poll() is not None]
        for p, (sym, day, lg, ts) in list(running.items()):
            if p not in fin and time.time() - ts > WORKER_TIMEOUT:
                log(f"TIMEOUT {sym} {day} kill"); p.kill(); fin.append(p)
        for p in fin:
            sym, day, lg, ts = running.pop(p)
            try: lg.close()
            except Exception: pass
            if not commit_day(sym, day):
                skips.append(f"{sym} {day}")
            done += 1
            write_status(done, total, f"{sym} {day}", t0, skips, len(running))
            log(f"done {sym} {day} ({done}/{total}, {(time.time()-t0)/done:.0f}s/symday avg, {len(jobs)} queued)")
        time.sleep(3)
    write_status(done, total, "COMPLETE", t0, skips, 0)
    git("add", "-f", "data_raw/bloomberg/STATUS.md"); git("commit", "-q", "-m", "data(daybot): option backfill complete"); git("push", "origin", BR)
    log(f"POOL COMPLETE: {done} symbol-days in {(time.time()-t0)/60:.0f} min")


if __name__ == "__main__":
    main()
