# Day-bot intraday OPTION pull — manifest (Bloomberg lab, 2026-07-03; autonomous run)

The first real intraday **option chain + side-classified tape** for the Day-Trading-Bot — the data
S1 (dealer-gamma + OFI) and S2 (0DTE vol-risk-premium) have never run on. SPY / SPX(SPXW) / QQQ,
recent-first over Bloomberg's ~140-calendar-day intraday cap. **Bloomberg reads only.**

## Entitlement (STEP 0 probe) — PASSED
Tested a near-ATM 0DTE + a monthly + a live daily on 2026-07-02:
| Underlying | bdib (1-min OHLCV, incl. BID/ASK bars) | bdtick TRADE/BID/ASK | Verdict |
|---|---|---|---|
| **SPY** | ✅ | ✅ TRADE+BID+ASK (0DTE ATM put ~5.9k trades/30min) | full chain + tape |
| **QQQ** | ✅ (same equity-option entitlement) | ✅ | full chain + tape |
| **SPX / SPXW** | ✅ (ticker fmt `SPXW US MM/DD/YY C<K> Index`) | ✅ SPXW 0DTE has TRADE ticks (359/30min) | full chain + tape |
| SPX *monthly* | ✅ | ⚠️ BID/ASK only, ~0 TRADE intraday | (we use SPXW for 0DTE/weekly) |

`bdib` supports `typ='BID'`/`'ASK'` → per-minute NBBO from cheap bars drives the wide chain.
SPXW index options ARE tradeable intraday (my first probe used a wrong ticker format without `US`).

## Scope (decoupled widths — the key design choice)
| Product | Source | Strikes | Expiries | Cadence |
|---|---|---|---|---|
| **CHAIN** (IV / GEX spine) | `bdib` BID+ASK bars → mid → BS IV; `bdh OPEN_INT` | **ATM ± 10** | **0DTE + next weekly** | 1-min |
| **TAPE** (side-classified flow) | `bdtick` TRADE/BID/ASK → NBBO + Lee-Ready | **ATM ± 3** | **0DTE** | per-print |

- ATM = nearest listed strike to the day's **open** spot (SPY/QQQ $1 increment; SPX $5).
- Window: recent-first from **2026-07-02 back to ~2026-02-13** (the ~140-day intraday cap; older has
  aged out of Bloomberg — that's a Theta+parity job, out of scope here).
- Priority within each recent day: **SPY → SPX → QQQ** (S&P spine first; QQQ is the tail).

## Derivation (per symbol, day, expiry, strike, right)
- **Tape:** build NBBO from BID/ASK; attach to each TRADE the last quote **STRICTLY BEFORE** the
  trade ts (`merge_asof` `allow_exact_matches=False` — never the concurrent/after quote → no
  look-ahead). Lee-Ready: quote rule vs mid, tick-test on ties → `side_inferred ∈ {buy,sell,mid}`
  (**buy = customer buy-initiated = dealer sells**). Raw quote ticks (~2.3M/contract) folded then
  **discarded** (never committed).
- **Chain IV:** invert **European** Black-Scholes from the per-minute mid, **r = 0.04, q = 0**
  (matches the engine's `config.risk_free_rate` and its dividend-free BS → IV is round-trip
  consistent with the engine's pricer). IV = **NaN** (never a garbage 500% row) when time-value <
  ~½¢ (deep ITM/OTM), vega≈0, or T is tiny (last minutes of 0DTE).
- **PIT spot** for a snapshot at minute t = **close of the bar ENDING at t** = `bars_1m[t−1].close`
  (bars are START-labelled, so `bars_1m[t].close` is ~1 min in the future — using it look-aheads
  into IV/GEX; we do not). 09:30 snapshot uses that bar's open.
- `available_ts` = the print/snapshot ts in UTC (arrival latency added at ingest, not here).

## Emitted schemas (engine-canonical — `intraday/contracts.py`), gzipped, one per (symbol,day)
- `option_chain/<SYM>_<YYYY-MM-DD>.csv.gz` — `snapshot_ts, available_ts, expiration, strike,
  option_type, open_interest, implied_vol, spot` (8 canonical) **+ bonus** `bid, ask, mid`.
- `option_tape/<SYM>_<YYYY-MM-DD>.csv.gz` — `ts, available_ts, expiration, strike, right, price,
  size, nbbo_bid, nbbo_ask, side_inferred`.
- (bonus) `underlying_ofi/<SYM>_1m.csv` — `ts, buy_vol, sell_vol, ofi` (Lee-Ready on underlying
  trades). **Deferred** — see Autonomous decisions.

`data_raw/` is gitignored → derived deliverables are force-committed (`git add -f`); raw ticks never.

## Validation gate (fail-safe, run on SPY 0DTE before the backfill)
Asserts (a) BID≤TRADE≤ASK on the majority of prints; (b) ATM 0DTE IV in [0.02, 3.0]; (c) side
split not ~100% one-sided; (d) a shift-by-one-bar spot test CHANGES the IV (proves PIT / no
look-ahead). On failure → `FAILED_VALIDATION.md` + HALT (a one-day halt is correct; a corrupted
140-day run is not). Unit tests (BS↔IV round-trip, Lee-Ready, strict-before NBBO, PIT) pass.
Early single-day check (SPY 2026-07-02): tape 95% inside NBBO, side 50/50, ATM 0DTE IV ~22% (VIX-scale).

## Caveats
- **American vs European IV:** SPY/QQQ options are American-exercise but we invert with **European**
  BS (matching the engine's pricer) — a small approximation for deep-ITM/early-exercise names.
  **SPX (SPXW) is truly European → clean.** This is the intended engine-consistent convention.
- **Lee-Ready proxy vs Theta:** `side_inferred` is a **Lee-Ready quote/tick inference**, not the
  exchange's true aggressor flag. The full ATM±10 all-expiry exchange-side-classified tape is a
  **Theta** pull; this scoped **ATM±3 / 0DTE Lee-Ready** tape is the Bloomberg down-payment.
- **>140-day history** and the **wide side-classified tape** are Theta + put-call-parity, out of scope.

## Autonomous decisions (operator was AFK — conservative, PIT-safe defaults)
1. **Tape scoped to 0DTE only** (dropped "next weekly" tape). 0DTE is the stated priority; this
   bounds the expensive bdtick stream and maximizes the number of recent DAYS covered (recent-first
   value). Chain keeps 0DTE + next weekly (cheap via bdib) for GEX term structure.
2. **SPX included via SPXW** (0DTE/weekly), $5 strikes, `Index` yellow-key — the European gamma
   spine the priority list calls for; confirmed tradeable intraday.
3. **"next weekly" = next Friday** strictly after the day (Thursday if that Friday is a holiday).
4. **r = 0.04 flat, q = 0** (no `treasury_yields.csv`/dividend file in-repo; matches engine BS;
   negligible at 0DTE). Bar-close IV uses the PIT (shift-by-one) spot.
5. **underlying_ofi deferred:** scripts written + unit-tested (`pull_underlying_ofi.py`), but the
   single Bloomberg stream is dedicated to the higher-priority option backfill. Run it per day when
   the stream frees; it has no engine consumer yet (engine derives OFI from the option tape).
6. **GitHub credentials left intact** (run keeps pushing); teardown deferred to operator's return.

## Coverage / status
Live progress in **`STATUS.md`** (last day, %complete, ETA, skips) and the branch git log
(`data(daybot): option tape+chain <SYM> <date> [pool]`, one commit per symbol-day). Producers:
`pull_options.py`, `option_derive.py`, `option_pool.py`, `validate_gate.py`, `pull_underlying_ofi.py`.
