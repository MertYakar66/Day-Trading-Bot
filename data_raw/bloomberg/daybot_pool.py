"""Day-bot PULL 1 — concurrent work-pool (recent-first, K parallel symbol-day streams).

Generalizes the proven 2-stream concurrency to K streams with MEMORY ADMISSION CONTROL: it never
starts a new worker while free RAM is below MIN_LAUNCH_AVAIL, so it auto-throttles K->2->1 under
pressure. Combined with each worker's adaptive 120-min window + the <2 GB split-guard, the box
can't OOM even at K=3 on the heaviest days. Each worker writes an ISOLATED JSON; the single
coordinator process is the only writer of spread_stats.csv (merge a day's two symbols -> commit +
push) so there is no CSV write race. Idempotent / resumable (skips done (symbol,date)); one retry
per transient worker error; per-worker hard timeout so a hung Bloomberg stream can't stall the
unattended run. The serial orchestrator (daybot_ticks.sh) is left intact for one-step revert.

Usage: python daybot_pool.py [K]    (K default = adaptive from free RAM, capped at 3; K=0 = dry run)
"""
import os
import sys
import json
import time
import subprocess
import importlib.util
from collections import deque, defaultdict
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pss", os.path.join(HERE, "pull_spread_stats.py"))
pss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pss)
mem_avail_gb, COLS, OUTF = pss.mem_avail_gb, pss.COLS, pss.OUTF

PY = sys.executable
WORKER = os.path.join(HERE, "_exp_worker.py")
TMPDIR = os.path.join(HERE, "_pool_tmp")
REPO = os.path.dirname(os.path.dirname(HERE))   # ...\Day-Trading-Bot
BR = "claude/bloomberg-intraday-pull"
SYMS = ["SPY", "QQQ"]
MIN_LAUNCH_AVAIL = 3.0     # GB; do not start a new worker below this (lowered for K=4 on the idle box;
                           # the hard 2.0 GB per-window split-guard in pull_spread_stats remains the OOM backstop)
WORKER_TIMEOUT = 3600      # s; kill a truly-hung worker. Raised 1500->3600: the heavy Q1 days
                           # legitimately take ~25-40 min/symbol at K=4, so 1500 was killing real
                           # workers mid-pull; 3600 still catches a genuine hang (which runs forever)
START, END = "2026-01-28", "2026-06-17"


def log(m):
    print(f"[pool] {m}", flush=True)


def done_set():
    if os.path.exists(OUTF):
        ex = pd.read_csv(OUTF)
        return set(zip(ex["symbol"], ex["date"].astype(str)))
    return set()


def git(*args, timeout=120):
    """Run a git op with a HARD TIMEOUT so a hung push can never freeze the single coordinator
    (it would otherwise block commits AND worker-reaping). On timeout the commit stays local and the
    9-min flush watchdog pushes it next cycle."""
    try:
        subprocess.run(["git", *args], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
                       timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        print("[pool] WARN: git timed out (continuing; flush watchdog will sync GitHub next cycle)", flush=True)
        return False
    except Exception as e:
        print(f"[pool] WARN: git {type(e).__name__} (continuing)", flush=True)
        return False


def has_staged():
    try:
        return subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO, timeout=30).returncode != 0
    except Exception:
        return False


def reconcile_pending():
    """Commit+push any spread_stats.csv rows a prior crashed run wrote but never committed (closes the
    to_csv->commit atomicity gap so no row is ever stranded local-only)."""
    git("add", "-f", OUTF)
    if has_staged():
        git("commit", "-q", "-m", "data(daybot): reconcile pending spread-stats on pool start")
        git("push", "origin", BR)
        print("[pool] reconciled pending CSV rows from a prior run", flush=True)


def commit_day(day, rows):
    if not rows:
        return
    allrows = (pd.concat([pd.read_csv(OUTF), pd.DataFrame(rows)], ignore_index=True)
               if os.path.exists(OUTF) else pd.DataFrame(rows))
    for c in COLS:
        if c not in allrows.columns:
            allrows[c] = pd.NA
    allrows = allrows[COLS].sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    allrows.to_csv(OUTF, index=False)
    git("add", "-f", OUTF)
    git("commit", "-q", "-m", f"data(daybot): PULL 1 spread-stats {day} (SPY,QQQ) [pool]")
    git("push", "origin", BR)
    log(f"committed+pushed {day} (+{len(rows)} rows -> {len(allrows)} total)")


def main():
    os.makedirs(TMPDIR, exist_ok=True)
    avail0 = mem_avail_gb()
    K = int(sys.argv[1]) if len(sys.argv) > 1 else (3 if avail0 > 6 else 2 if avail0 > 4 else 1)
    reconcile_pending()
    done = done_set()
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(START, END)[::-1]]
    jobs = deque()
    expected = {}
    for day in days:
        todo = [s for s in SYMS if (s, day) not in done]
        if todo:
            expected[day] = set(todo)
            for s in todo:
                jobs.append((day, s))
    log(f"start K={K} avail={avail0:.1f}GB | {len(jobs)} tasks across {len(expected)} days")
    if K == 0:                       # dry run: validate scheduling without Bloomberg
        log(f"DRY first tasks: {list(jobs)[:6]}")
        log(f"DRY last tasks:  {list(jobs)[-4:]}")
        return

    running = {}                     # proc -> (day, sym, outpath, t_start)
    results = defaultdict(dict)      # day -> {sym: row}
    retries = defaultdict(int)
    t0 = time.time()
    n_done_days = 0
    while jobs or running:
        # launch up to K, gated by memory admission control (always allow the 1st)
        while jobs and len(running) < K and (len(running) == 0 or mem_avail_gb() > MIN_LAUNCH_AVAIL):
            day, sym = jobs.popleft()
            outp = os.path.join(TMPDIR, f"{sym}_{day}.json")
            try:
                os.path.exists(outp) and os.remove(outp)
            except OSError:
                pass
            p = subprocess.Popen([PY, WORKER, sym, day, outp])
            running[p] = (day, sym, outp, time.time())
            log(f"launch {sym} {day} (running={len(running)}/{K} avail={mem_avail_gb():.1f}GB)")
        # collect finished + enforce timeouts
        finished = [p for p in running if p.poll() is not None]
        for p, (day, sym, outp, ts) in list(running.items()):
            if p not in finished and time.time() - ts > WORKER_TIMEOUT:
                log(f"TIMEOUT {sym} {day} -> kill (retry later)")
                p.kill()
                finished.append(p)
        for p in finished:
            day, sym, outp, ts = running.pop(p)
            row, err = None, "no-output"
            try:
                with open(outp) as f:
                    d = json.load(f)
                row, err = d.get("r"), d.get("err")
            except Exception as e:
                err = f"read-fail {e}"
            if row:
                results[day][sym] = row
                expected[day].discard(sym)
            elif err and retries[(day, sym)] < 1:      # retry only genuine errors, not empty/holiday
                retries[(day, sym)] += 1
                jobs.append((day, sym))
                log(f"retry {sym} {day} (err={err})")
            else:
                log(f"{'GIVEUP' if err else 'empty'} {sym} {day} (err={err})")
                expected[day].discard(sym)
            if day in expected and not expected[day]:
                commit_day(day, list(results.get(day, {}).values()))
                expected.pop(day, None)
                results.pop(day, None)
                n_done_days += 1
                log(f"day {day} done ({n_done_days} committed, "
                    f"~{(time.time()-t0)/n_done_days:.0f}s/day avg)")
        time.sleep(2)
    log(f"POOL COMPLETE: {n_done_days} days in {(time.time()-t0)/60:.0f} min")


if __name__ == "__main__":
    main()
