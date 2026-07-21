# Option backfill — STATUS (autonomous run)

**288/288 symbol-days done (100.0%)** | last committed: `COMPLETE` | in-flight: 0 | ~95s/symbol-day | ETA remaining ~0.0h (recent-first: newest days are banked first).

- Scope: SPY/SPX/QQQ, CHAIN ATM±10 (0DTE+next weekly, bdib IV), TAPE ATM±3 (0DTE, bdtick Lee-Ready). Window recent-first back to ~today−140.
- Deliverables: `option_chain/<SYM>_<date>.csv.gz`, `option_tape/<SYM>_<date>.csv.gz` (committed per symbol-day). Raw ticks discarded.
- Persistence: per-symbol-day commit+push; 10-min flush watchdog keeps local==origin.

_Updated mid-run — see OPTION_PULL_MANIFEST.md for the full coverage table & caveats._
