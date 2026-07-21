# Day-bot Bloomberg intraday pull — manifest (lab 2026-06-17; resumed 2026-06-18; rolled forward 2026-07-03)

> ## 2026-07-03 — rolled forward to current (bars + spread_stats → 2026-07-02)
> **Changelog:** brought the ~2-week-stale capture current (run date 2026-07-03; 07-03 itself is the
> Independence-Day-observed holiday, market closed). **BARS:** ran `pull_bars.py` for 2026-06-18→07-02
> (24 ETFs/equities + SPX Index, 1-min RTH) and appended + deduped (on `ts`) into the per-symbol
> `bars_1m/<SYM>_1m.csv` — every file now spans 2026-01-28..**2026-07-02** (108 trading days,
> ~42.3k bars/sym; SPX ~44.3k), monotonic UTC, canonical headers, zero missing days.
> **SPREAD-STATS:** derived SPY+QQQ quoted/effective/TWA spread for 2026-06-18→07-02 via the streaming
> deriver under `daybot_pool.py` (K=4 concurrent w/ memory admission control + per-day commit/push,
> backed by a 10-min flush watchdog). `spread_stats.csv` now = **218 rows** (SPY+QQQ × **109 trading
> days**, 2026-01-27..**2026-07-02**), **zero gaps**. Validation on a fresh SPY tick day: median quoted
> spread **0.268 bps** (matches banked ~0.265), BID≤TRADE≤ASK **94.5%** inside NBBO, timestamps
> UTC + monotonic (after the deriver's per-window sort).
> **Gap-fill (task 2b):** the 2026-01-27..06-17 window was already complete — the only absent dates are
> 3 market holidays (02-16, 04-03, 05-25), correctly empty; nothing to backfill.
> **Real-market note:** a genuine late-June widening episode — SPY ~0.27→~0.41 and QQQ ~0.54→~0.84 bps
> on 06-24..06-29 (both symbols, reverting after) — lifts the new-day medians (SPY 0.271 / QQQ 0.633
> bps) above the Jan–Jun medians (0.265 / 0.306). Intraday ticks older than ~140 cal days (pre-~Feb-13)
> have aged out of Bloomberg and can't be re-pulled — already banked.
> **Deferred (optional):** extending `spread_stats.csv` to the full 24-symbol universe (SPY/QQQ only for
> now) — a ~100+ h single-stream pull on this box, not attempted this session.

> ## 2026-06-18 — moved to the CORRECT repo (Day-Trading-Bot) + resumed
> **De-contamination:** this pull now lives in **`MertYakar66/Day-Trading-Bot`** (branch
> `claude/bloomberg-intraday-pull`), NOT the wheel repo. The earlier work accidentally landed on the
> wheel's `claude/daybot-bloomberg-pull` branch; the 3 pullers + 15 already-pulled tick gz were
> carried over here.
> **Entitlement re-confirmed (fresh session):** `bdtick` (NBBO+trades) and `bdib` (1-min bars, incl
> `SPX Index`) both live — SPY 5-min slice = 101,543 ticks, spread ~$0.04 ≈ 0.5 bps.
> **Storage decision (Option A — this repo gitignores `data_raw/` by convention):**
> raw ticks are perishable + gitignored (local working set); the **durable committed deliverable is
> `spread_stats.csv`** (per symbol-day quoted/effective/time-weighted spread, $+bps) — it replaces the
> day-bot's 5 bps fallback. The 15 already-pulled raw gz (469 MB) were force-committed as the carried
> archive; **new backfill raw stays local (never the ~6 GB in git, never the wheel repo)**.
> **Spread findings (15 days):** SPY real spread ≈ 0.14–0.41 bps (eff 0.13–0.27), QQQ ≈ 0.16–0.86 bps
> → the 5 bps guess overstates SPY costs ~10–15×.
> **Resume status 2026-06-18:** PULL 3 events ✅ (12 events incl PPI, committed). PULL 2 bars ✅/▶
> (per-symbol commit+push, in progress). PULL 1 ticks ▶ recent-first per-day (pull→derive stats→commit
> +push), running as a background orchestrator. New raw ticks local-only (Option A).

Branch (original, in wheel repo — contamination) `claude/daybot-bloomberg-pull`. **Separate concern from the wheel** — intraday + event data
the day-trading engine needs. Bloomberg intraday history is capped at ~140 calendar days (perishable)
→ pulling the full window now. Lab PC is ephemeral; persisted to GitHub. Reproducible producers
committed (the spec calls out connector files that lacked a producer — not repeating that).

## Pipeline validated ✓
`bdib` (1-min bars) and `bdtick` (NBBO+trades) both work and are **entitled**. Both return **correct
UTC** (no conversion); bars are **start-labelled**. Canonical, explicit headers enforced (the spec's
#1 rule, given `sp500_ohlcv.csv`'s rotated-column history). Validated on `SPY_ticks_2026-06-10`:
8,094,823 ticks, cols `ts,type,value,size`, UTC open `13:30:00.001Z`→close `20:00:00.927Z`, types
BID 3.94M / ASK 3.75M / TRADE 0.40M, NBBO sane (BID 731.00 < ASK 731.15 → measurable spread).

## The three pulls
| # | producer | output | scope | est |
|---|---|---|---|---|
| **1 — ticks** (highest) | `pull_ticks.py SPY,QQQ <start> <end>` | `ticks/<SYM>_ticks_<date>.csv.gz` (per symbol-day) | SPY+QQQ × ~101 trading days (2026-01-27→06-17) | ~8–16 h, ~6 GB |
| **2 — bars** | `pull_bars.py <start> <end>` | `bars_1m/<SYM>_1m.csv` (one per symbol) | 24 ETFs/equities + SPX Index, 1-min RTH | ~1.5 h, ~75 MB |
| **3 — events** | `pull_events.py` | `events/macro_calendar.csv` | 12 macro events (+PPI), date+time-ET+survey/actual/prior | ~5 min |

Run order = priority (ticks → bars → events), one terminal request stream (no concurrency).

## Hygiene
- **UTC** timestamps, explicit (`ts` ISO with `+0000`); bars start-labelled. Events store ET time +
  an explicit `tz_label` column.
- **Canonical headers** written explicitly, not Bloomberg field order.
- RTH = 09:30–16:00 America/New_York → UTC per date (handles the EST↔EDT boundary).
- `pull_ticks.py` is **resumable** (skips completed `.csv.gz`) + **atomic** (`.tmp`→rename, so an
  interruption never leaves a corrupt file) — safe for an unattended multi-hour run.

## Storage decision
Lab PC ephemeral + GitHub preferred → committed to this branch. Google Drive via MCP isn't viable for
multi-GB binaries. Ticks are per-symbol-day gz (each ~27–44 MB, under GitHub's 100 MB file limit);
committed incrementally as they land. **Recommendation:** the full ~6 GB tick set ideally migrates to
a standalone day-bot repo (or Git LFS) rather than permanently growing the wheel remote — flagged for
follow-up.

## NOT pulled (per spec — already have / wrong tool)
VIX term structure / futures / vol indices (wheel), 503-name EOD panels (wheel), intraday option
chain + tape (Theta, not Bloomberg).

## Run status
Facilitated run with ~30-min health checks; incremental commits per batch. See git log of this branch.
