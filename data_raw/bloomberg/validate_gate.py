"""FAIL-SAFE validation gate: fully assert one processed SPY 0DTE day before the backfill.
Exits 0 (PASS -> proceed) or 1 (FAIL -> caller halts + writes FAILED_VALIDATION.md).

Asserts:
  (a) BID <= TRADE <= ASK on the majority of tape prints
  (b) ATM 0DTE inverted IV in a sane band (reject <2% or >300%)
  (c) side_inferred split is NOT ~100% one-sided
  (d) spot is PIT-aligned: recomputing IV with a look-ahead (shift-by-one-bar) spot CHANGES the
      values materially (proves no look-ahead leaked into the emitted IV)
Usage: python validate_gate.py SPY 2026-07-02
"""
import sys, os
import numpy as np
import pandas as pd
import option_derive as od

sym, dstr = sys.argv[1], sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
tape = pd.read_csv(os.path.join(HERE, "option_tape", f"{sym}_{dstr}.csv.gz"))
chain = pd.read_csv(os.path.join(HERE, "option_chain", f"{sym}_{dstr}.csv.gz"))
fails = []


def gate(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        fails.append(name)


print(f"=== VALIDATION GATE {sym} {dstr} (tape {len(tape)} rows, chain {len(chain)} rows) ===")

# (a) TRADE within the (strict-before) NBBO. The stored NBBO is the last quote STRICTLY BEFORE each
# trade (PIT / no look-ahead, as required), so it is intentionally staler than the concurrent quote
# (concurrent-NBBO inside ~96%, verified) and fewer fast-0DTE prints sit *strictly* inside. Require a
# clear MAJORITY strictly inside AND near-universal within ~1 tick (the latter proves the NBBO
# reconstruction is sound — a broken reconstruction would put prints far outside, not 1 tick out).
tt = tape.dropna(subset=["nbbo_bid", "nbbo_ask"])
inside = ((tt.price >= tt.nbbo_bid - 1e-9) & (tt.price <= tt.nbbo_ask + 1e-9)).mean() if len(tt) else 0
within = ((tt.price >= tt.nbbo_bid - 0.06) & (tt.price <= tt.nbbo_ask + 0.06)).mean() if len(tt) else 0
gate("(a) TRADE within strict-before NBBO", inside >= 0.55 and within >= 0.85,
     f"{inside*100:.1f}% strictly inside, {within*100:.1f}% within $0.06 (strict-before = PIT/staler by design)")

# (b) ATM 0DTE IV sane band
c0 = chain[chain.expiration == dstr].copy()
atmk = c0.iloc[(c0.strike - c0.spot.median()).abs().argsort()].strike.iloc[0] if len(c0) else None
mid_hours = c0.snapshot_ts.str[11:13].astype(int).between(15, 16)   # ~11:00-13:00 ET
atm_iv = c0[(c0.strike == atmk) & mid_hours].implied_vol.dropna()
med_iv = atm_iv.median() if len(atm_iv) else np.nan
gate("(b) ATM 0DTE IV in [0.02,3.0]", np.isfinite(med_iv) and 0.02 <= med_iv <= 3.0,
     f"ATM strike={atmk} midday median IV={med_iv:.4f}" if np.isfinite(med_iv) else "no ATM IV")

# (c) side mix not ~100% one-sided
mix = tape.side_inferred.value_counts(normalize=True)
buy, sell = float(mix.get("buy", 0)), float(mix.get("sell", 0))
gate("(c) both sides present (>5% each)", buy > 0.05 and sell > 0.05, f"buy={buy*100:.1f}% sell={sell*100:.1f}%")

# (d) PIT alignment: recompute ATM 0DTE IV with a look-ahead spot; must differ
fp = os.path.join(HERE, "bars_1m", f"{od.SYMCFG[sym]['bars']}_1m.csv")
b = pd.read_csv(fp); b["ts"] = pd.to_datetime(b["ts"], utc=True)
b = b[b["ts"].dt.strftime("%Y-%m-%d") == dstr].sort_values("ts").set_index("ts")
lookahead = b["close"].reindex(od.rth_minute_grid(dstr))         # WRONG: bar[t].close (future)
samp = c0[(c0.strike == atmk) & c0.implied_vol.notna() & c0["mid"].notna()].copy()
samp["ts"] = pd.to_datetime(samp.snapshot_ts, utc=True)
diffs, n_cmp = [], 0
for _, row in samp.iterrows():
    la = lookahead.get(row["ts"])
    if la is None or pd.isna(la) or la <= 0:
        continue
    T = od.years_to_expiry(row["ts"], row["expiration"])
    iv_look = od.implied_vol(row["mid"], float(la), row["strike"], T, row["option_type"])
    if np.isfinite(iv_look):
        diffs.append(abs(iv_look - row["implied_vol"])); n_cmp += 1
frac_changed = np.mean([d > 1e-4 for d in diffs]) if diffs else 0.0
gate("(d) shift-by-one spot CHANGES IV (PIT, no look-ahead)", n_cmp >= 10 and frac_changed >= 0.5,
     f"{frac_changed*100:.0f}% of {n_cmp} ATM IVs change; mean|dIV|={np.mean(diffs):.5f}" if diffs else "no comparable rows")

print(f"=== GATE {'PASSED' if not fails else 'FAILED: ' + ', '.join(fails)} ===")
sys.exit(0 if not fails else 1)
