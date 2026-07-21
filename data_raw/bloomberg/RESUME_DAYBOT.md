# Day-bot PULL 1 — resume card (updated 2026-06-18, K=4 config)

**If the box closed mid-backfill, this is how to resume with ZERO committed-data loss.**

## What's safe vs what's lost on a close
- **Committed days are on GitHub** (branch `claude/bloomberg-intraday-pull`, file
  `data_raw/bloomberg/spread_stats.csv`). Two push paths: the pool commits+pushes per day, and a
  **9-min flush watchdog** (`bbg_work/daybot_flush.sh`, logs to `bbg_work/flush.log`) re-pushes any
  lag. Safety is auditable: `flush OK: GitHub==local @ <sha>` every 9 min.
- **Maximum loss on a close = the in-flight days only** (workers don't commit until both symbols of
  a day finish). They are simply re-pulled on resume — idempotent, nothing corrupts.

## State at writing
- Deliverable: per (symbol, day) **quoted / effective / time-weighted spread** stats, recent-first
  over business days 2026-01-28 .. 2026-06-17. Replaces the day-bot's 5 bps fallback.
  Findings so far: **SPY ~0.27 bps, QQQ ~0.42 bps** median quoted spread (vs the 5 bps guess ~12–18×).
- **✅ COMPLETE — 2026-06-19 ~07:00 UTC.** All 98 in-range trading days banked for SPY+QQQ (99
  distinct days incl. carried 01-27; 198 symbol-days). The only target days not present are the 3
  market holidays (02-16 Presidents', 04-03 Good Friday, 05-25 Memorial), correctly empty — verified
  in the log, not errors. Zero real days missing.
- **Final calibration** (replaces the 5 bps fallback, ~16-19x overstatement): SPY quoted **0.265 bps**
  / eff 0.145 / TWA 0.239; QQQ quoted **0.306 bps** / eff 0.165 / TWA 0.310.
- Ran as a concurrent work-pool. Final config: **K=4, WINDOW_CAP=90, WORKER_TIMEOUT=3600**, admission
  floor 3.0 GB / split-guard 2.5 GB. The heavy Q1 tail completed cleanly (final run: 47 days in ~6.8h).
- Nothing to resume. To refresh/extend later, the resume command below is still valid + idempotent.

## Resume command (one line)
```bash
# Prereqs: Bloomberg Terminal running + logged in (blpapi needs it); repo cloned/pulled.
bash /c/Users/mertmert/bbg_work/daybot_pool.sh 4      # K=4 (current best). Use 3 or 2 if RAM is tighter.
bash /c/Users/mertmert/bbg_work/daybot_flush.sh       # restart the 9-min push watchdog (background)
```
- Idempotent + resumable: skips (symbol,date) already in `spread_stats.csv`, continues recent-first.
- **Self-throttling / OOM-proof:** memory admission control won't start a new stream below **3.0 GB**
  free; each worker uses an adaptive **120/90/60-min** window and a **<2.0 GB split-guard** that halves
  a window under pressure; **1 retry** per transient error then defer to next run; **25-min** per-worker
  hang timeout. On heavy days K=4 auto-degrades to 3/2 — never OOMs, never loses data.
- Revert to the rock-solid serial path instead: `bash /c/Users/mertmert/bbg_work/daybot_ticks.sh`.

## Tuning knobs
- `daybot_pool.sh <K>` — concurrency (4 = current). `daybot_pool.py`: `MIN_LAUNCH_AVAIL` (3.0 GB
  admission floor). `pull_spread_stats.py`: `WINDOW_CAP` (120), `MEM_FLOOR_GB` (2.0 split-guard).

## Producers / scripts (all in data_raw/bloomberg/ unless noted)
- `daybot_pool.py` — K-concurrent coordinator (single CSV writer; per-day commit+push).
- `_exp_worker.py` — isolated per-(symbol,day) pull worker (writes its own JSON; no CSV race).
- `pull_spread_stats.py` — streaming spread deriver (adaptive window + memory guards + instrumentation).
- `daybot_pool.sh` / `daybot_ticks.sh` (concurrent / serial launchers), `daybot_flush.sh` (9-min flush)
  — all in `C:\Users\mertmert\bbg_work\`.

## Storage decision (Option A — unchanged)
Raw ticks are perishable + gitignored (local only, disposable). The durable committed deliverable is
**`spread_stats.csv`** (small). Never put raw ticks (~GBs) in git; never in the wheel repo. No Google
Drive needed — the durable output is tiny and lives on GitHub.

## Then continue to the rest of the day-bot plan
PULL 2 (bars) and PULL 3 (events) are already done + pushed. Once PULL 1 reaches zero remaining, the
day-bot Bloomberg pull is complete. See `SESSION_2026-06-18_SUMMARY.md` for the full picture.
