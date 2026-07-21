"""Day-bot PULL 3 — macro event calendar (exact release timestamps). Bloomberg lab 2026-06-17.

For each macro event: release SCHEDULE (date + TIME in ET) via bds ECO_FUTURE_RELEASE_DATE_LIST
(recent + forward ~12mo), merged with the ACTUAL-value history via bdh PX_LAST (back to 2015) and
the current consensus SURVEY (bdp). 'prior' = the previous actual. Times are EXCHANGE-LOCAL (ET) as
Bloomberg's ECO calendar reports them — stored with an explicit tz_label column so the day-bot
converts to UTC unambiguously (CPI/PPI/NFP 08:30 ET, ISM 10:00 ET, FOMC 14:00 ET).

Output: data_raw/bloomberg/events/macro_calendar.csv
Columns: event, ticker, name, release_date, release_time, tz_label, survey, actual, prior
Usage:  python pull_events.py
"""
import os
import pandas as pd
from xbbg import blp

OUT = os.path.join(os.path.dirname(__file__), "events")
# (ticker, event label) — adds PPI vs the wheel set
EVENTS = [
    ("FDTR Index", "fomc_rate_decision"), ("CPI YOY Index", "cpi"), ("CPI XYOY Index", "core_cpi"),
    ("PPI YOY Index", "ppi"), ("FDIUFDYO Index", "ppi_final_demand"),
    ("NFP TCH Index", "nonfarm_payrolls"), ("USURTOT Index", "unemployment_rate"),
    ("PCE CYOY Index", "pce"), ("GDP CQOQ Index", "gdp"), ("RSTAMOM Index", "retail_sales"),
    ("NAPMPMI Index", "ism_manufacturing"), ("NAPMNMI Index", "ism_services"),
]


def native(x):
    return x.to_native() if hasattr(x, "to_native") else x


rows = []
for tkr, label in EVENTS:
    try:
        meta = native(blp.bdp([tkr], ["NAME", "BN_SURVEY_MEDIAN"]))
        m = {r["field"]: r["value"] for _, r in meta.iterrows()} if len(meta) else {}
        name, survey = m.get("NAME", ""), m.get("BN_SURVEY_MEDIAN", "")
    except Exception:
        name, survey = "", ""
    # actual-value history (date -> actual), build prior via shift
    actual_by_date = {}
    try:
        h = native(blp.bdh(tkr, ["PX_LAST"], "2015-01-01", "2026-06-17"))
        if {"date", "value"}.issubset(h.columns):
            hh = h.dropna(subset=["value"]).sort_values("date")
            vals = hh["value"].tolist()
            for i, (_, r) in enumerate(hh.iterrows()):
                actual_by_date[pd.to_datetime(r["date"]).strftime("%Y-%m")] = (round(float(r["value"]), 4),
                    round(float(vals[i - 1]), 4) if i else None)
    except Exception:
        pass
    # schedule (date + ET time)
    try:
        d = native(blp.bds(tkr, "ECO_FUTURE_RELEASE_DATE_LIST"))
        col = next((c for c in d.columns if "Release" in c or "Date" in c), None)
        n = 0
        if col:
            for dt in pd.to_datetime(d[col], errors="coerce").dropna():
                ym = dt.strftime("%Y-%m")
                actual, prior = actual_by_date.get(ym, (None, None))
                rows.append({"event": label, "ticker": tkr, "name": name,
                             "release_date": dt.strftime("%Y-%m-%d"), "release_time": dt.strftime("%H:%M:%S"),
                             "tz_label": "America/New_York", "survey": survey, "actual": actual, "prior": prior})
                n += 1
    except Exception as e:
        n = f"ERR {type(e).__name__}"
    print(f"  {label:20} {tkr:16} scheduled={n}  ({name})")

os.makedirs(OUT, exist_ok=True)
df = pd.DataFrame(rows).sort_values(["release_date", "event"])
df.to_csv(os.path.join(OUT, "macro_calendar.csv"), index=False)
print(f"\nmacro_calendar.csv: {len(df)} rows, {df['event'].nunique()} events, "
      f"{df['release_date'].min()}..{df['release_date'].max()}; release times {sorted(df['release_time'].unique())}")
