# Bloomberg data session — 2026-06-18 (full-day work log)

End-of-day documentation of everything done this session, across both repos. Authored as the user
left the box running unattended; the day-bot PULL 1 backfill continues autonomously (see
`RESUME_DAYBOT.md`).

---

## 1. Wheel broad-pull (`MertYakar66/smart-wheel-engine`) — COMPLETE ✅
Branch `claude/bloomberg-broad-pull-2026-06-17`. Pulled every remaining catalog item (BDH/BDP/BQL),
FLDS-verified by non-null value count, validated (sane bands + overlap-to-the-cent vs existing
vintages), staged, gz'd anything >100 MB, committed+pushed per dataset. **Manifest reached ZERO**
(~20 datasets): currency refresh, VIX futures curve, short interest, dividend-yield PIT, macro
cross-asset + sector/factor ETFs, per-name daily/low-freq panels, snapshots, forward estimates.
Notable catches: KLAC 10:1 split, WTI −37.6 historical print, dividend "#354 = no-dividend-not-missing".
Rails honored: Bloomberg pulls only, staged for review, no engine/integration wiring, no §2 weakening.

## 2. Day-bot intraday pull (`MertYakar66/Day-Trading-Bot`) — branch `claude/bloomberg-intraday-pull`
De-contaminated into the **day-bot's own repo** (earlier work had landed in the wheel repo by mistake).
Three pulls:

| # | what | status |
|---|---|---|
| **3 — events** | macro calendar, 12 events incl PPI (date + ET time + survey/actual/prior) | ✅ done + pushed |
| **2 — bars** | 1-min RTH bars, 24 ETFs/equities + SPX Index, 2026-01-28..06-17 | ✅ done + pushed |
| **1 — ticks → spread stats** | per (symbol,day) quoted/effective/TWA spread for SPY+QQQ | ▶ in progress (this session's focus) |

### PULL 1 — the spread-calibration backfill (the day's main engineering)
**Goal:** real intraday spreads to replace the day-bot's 5 bps fallback. **Finding so far:**
**SPY ~0.27 bps, QQQ ~0.42 bps** median quoted spread — the 5 bps guess overstates costs ~12–18×.

**Storage (Option A):** a full-day raw `bdtick` is ~8–11 M ticks (~9 GB) and OOM'd the 16 GB box, so
raw ticks are **disposable + gitignored**; the durable committed deliverable is the small
**`spread_stats.csv`**. Solved OOM with a **streaming deriver** (`pull_spread_stats.py`): pulls RTH in
windows, folds NBBO spread stats incrementally (peak ~1–2 GB), idempotent/resumable.

**The optimization arc (all measured, not assumed):**
1. **Window size.** Diagnosed the run as *latency/transfer-bound, not memory-bound* (workers ~0.3 GB
   while each `bdtick` window waited ~50–100 s). Made the window **adaptive to free RAM** (120/90/60-min)
   → 60→120 cut requests/day ~43%, measured **~11% faster** (77 vs 87 s/Mtick). Added per-window timing +
   peak-RSS instrumentation and a `<2.0 GB` split-guard.
2. **Concurrency K=2** (experiment on 05-21): two parallel streams genuinely overlap; ~10–27%
   per-stream contention tax but envelope ≈ slower stream → **~29% faster than serial** (envelope 546 s
   vs true-serial 764 s). Peak RAM 2.43 GB, zero errors.
3. **Concurrent work-pool, K=3** (`daybot_pool.py`): generalized to K streams with **memory admission
   control** (no new stream below the RAM floor), single-writer CSV (no race), per-day commit+push,
   1-retry + 25-min hang-timeout, recent-first/resumable. Measured **~11.4 days/hour** (~2× serial) —
   K=3 beats K=2 via *cross-day pipelining* (the 3rd stream advances the next day with no inter-day gap).
4. **K=4** (current): Bloomberg **accepts and serves 4 concurrent sessions in parallel** (per-stream
   rate ~70 s/Mtick, *not* throttled), zero errors, memory comfortable. Measured **~16.5 days/hour**
   (~45% faster than K=3, ~2.9× serial). Admission floor lowered to 3.0 GB to let the 4th stream launch;
   2.0 GB split-guard remains the OOM backstop.

**Chosen config: K=4** (it strictly dominates K=3 — self-falls-back to 3 under any memory pressure).

## 3. Infrastructure / safety nets (for the unattended run)
- `daybot_pool.py` + `daybot_pool.sh <K>` — concurrent coordinator/launcher (running at K=4).
- `_exp_worker.py` — isolated per-(symbol,day) pull worker.
- `pull_spread_stats.py` — streaming deriver (adaptive window, memory guards, instrumentation).
- `daybot_flush.sh` → `bbg_work/flush.log` — **9-min GitHub flush watchdog** (redundant save path;
  logs `flush OK: GitHub==local @ <sha>`).
- `daybot_ticks.sh` — serial fallback (one-step revert).
- 20-min progress monitor (note: its "days/last-day" counter mis-reads *recent-first* backfilling —
  trust `spread_stats.csv` distinct-day count as ground truth; its errs/free fields are fine).
- Keep-awake: not used (the user is keeping the box awake ≥4 h).

## 4. State at handoff
- **~36 / ~102 target days banked, ~66 remaining (oldest 2026-01-29).** ETA ~4 h at 16.5 days/hr.
- **Data safety verified: local HEAD == GitHub HEAD; flush watchdog green.**
- Worst case on close = re-pull the in-flight day(s); committed progress never lost.

## 5. Resume
See **`RESUME_DAYBOT.md`** — one-line resume (`daybot_pool.sh 4` + `daybot_flush.sh`), idempotent.

## 6. Security / housekeeping (carry-over)
- Never paste a PAT into chat; pushes use Git Credential Manager browser-OAuth (cached, non-interactive).
- Teardown when fully done: revoke the GitHub GCM browser-OAuth grant on this shared lab box.
