"""S2 VRP workstream — part (e): end-to-end S2 input path on a REAL chain.

Builds an :class:`intraday.contracts.OptionChainSeries` from the REAL Theta
iv_surface snapshot ``AAPL_20260601.parquet`` (smart-wheel-engine, READ-ONLY),
pre-filtered to the SINGLE nearest expiry (audit caveat #1: atm_iv_at blends
expiries if handed a multi-expiry frame), then calls
``intraday.features.vrp.vrp_at`` with real store_yahoo 5m bars.

POINT-IN-TIME: the snapshot's underlying_price (306.43) matches the store's
2026-06-01 close (306.32), NOT the 2026-05-29 close (312.07) — so this snapshot
is as-of the 2026-06-01 EOD and is tradable from the NEXT session open,
2026-06-02 09:30 ET. available_ts is set there. The bar store ends 2026-06-01,
so at the 2026-06-02 decision time the PIT-available bars are the full
2026-06-01 session — the RV term is the prior session's trailing-31-bar RV
(what a live bot would hold at the next open before new bars accumulate).
Nothing same-day informs the snapshot's own day.

Column mapping (iv_surface -> CHAIN_COLUMNS): right->option_type,
iv->implied_vol, expiration->expiration; spot from the same-date chains file's
underlying_price; open_interest is NOT in iv_surface -> set 0 (unused by
atm_iv_at/vrp_at; disclosed).

Output: data_raw/swe_tests/vrp_e2e_result.json

Run:  .venv/Scripts/python.exe scripts/swe_tests/vrp_s2_e2e.py
(cwd = repo root)
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
for p in (str(REPO), str(REPO / "vendor" / "swe")):
    if p not in sys.path:
        sys.path.insert(0, p)

from intraday.contracts import CHAIN_COLUMNS, DataSource, OptionChainSeries  # noqa: E402
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.features.vrp import vrp_at  # noqa: E402

OUT = REPO / "data_raw" / "swe_tests"
OUT.mkdir(parents=True, exist_ok=True)
ET = ZoneInfo("America/New_York")

SNAPSHOT_DAY = date(2026, 6, 1)      # snapshot as-of this session's close
NEXT_SESSION = date(2026, 6, 2)      # first session it may inform (PIT)


def ts_et(d: date, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm)), tz=ET).tz_convert("UTC")


def main() -> None:
    surf = pd.read_parquet(SWE / "data_processed/theta/iv_surface/AAPL_20260601.parquet")
    chains = pd.read_parquet(SWE / "data_processed/theta/chains/AAPL_20260601.parquet")
    spot = float(chains["underlying_price"].iloc[0])

    nearest_exp = surf["expiration"].min()
    one = surf.loc[surf["expiration"] == nearest_exp].copy()

    snapshot_ts = ts_et(SNAPSHOT_DAY, 16, 0)     # 2026-06-01 16:00 ET close
    available_ts = ts_et(NEXT_SESSION, 9, 30)    # next session open (PIT)

    frame = pd.DataFrame({
        "snapshot_ts": snapshot_ts,
        "available_ts": available_ts,
        "expiration": pd.Timestamp(nearest_exp).date(),
        "strike": one["strike"].astype(float),
        "option_type": one["right"].astype(str),
        "open_interest": 0,                       # not in iv_surface; unused here
        "implied_vol": one["iv"].astype(float),
        "spot": spot,
    })[list(CHAIN_COLUMNS)]
    chain = OptionChainSeries(symbol="AAPL", frame=frame, source=DataSource.THETA)

    # PIT guard: on the snapshot's own day the chain must NOT be available.
    same_day_probe = ts_et(SNAPSHOT_DAY, 15, 0)
    leaked = not chain.latest_available(same_day_probe).empty

    # Decision time: next session, 10:00 ET. PIT bars = everything closed by then
    # (the store ends 2026-06-01, so this is the full prior session).
    as_of = ts_et(NEXT_SESSION, 10, 0)
    store = ParquetStore(REPO / "data_raw" / "store_yahoo")
    bars = store.read_bars("AAPL", SNAPSHOT_DAY, "5m")
    assert bars.source.value == "yahoo"
    pit_bars = bars.available_at(as_of)

    v = vrp_at(chain, pit_bars, as_of, "5m")  # S2 defaults: window=30, garman_klass

    snap = chain.latest_available(as_of)
    nearest = (snap["strike"] - spot).abs().min()
    atm_strikes = sorted(snap.loc[(snap["strike"] - spot).abs() <= nearest + 1e-9, "strike"].unique())

    result = {
        "snapshot_file": "iv_surface/AAPL_20260601.parquet",
        "expiry_used": str(pd.Timestamp(nearest_exp).date()),
        "dte_at_snapshot": int(one["dte"].iloc[0]),
        "n_rows_single_expiry": int(len(frame)),
        "spot_from_chains_underlying_price": spot,
        "store_close_2026_06_01": float(bars.frame["close"].iloc[-1]),
        "store_close_2026_05_29": float(
            store.read_bars("AAPL", date(2026, 5, 29), "5m").frame["close"].iloc[-1]
        ),
        "snapshot_ts_utc": str(snapshot_ts),
        "available_ts_utc": str(available_ts),
        "as_of_utc": str(as_of),
        "pit_guard_same_day_leak": leaked,
        "n_pit_bars_at_as_of": int(len(pit_bars)),
        "atm_strikes_used": [float(s) for s in atm_strikes],
        "atm_iv": v.atm_iv,
        "rv": v.rv,
        "vrp": v.vrp,
        "notes": [
            "single nearest expiry pre-filtered (audit caveat #1)",
            "open_interest absent from iv_surface -> 0; unused by atm_iv_at/vrp_at",
            "RV term is the prior session's trailing 31x5m Garman-Klass RV because "
            "the bar store ends 2026-06-01; PIT-honest at the 06-02 open",
            "n=1 snapshot — EXECUTION PROOF, not an edge measurement",
        ],
    }
    with open(OUT / "vrp_e2e_result.json", "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    assert not leaked, "PIT VIOLATION: chain visible on its own snapshot day"
    assert v.vrp is not None, "end-to-end path failed to produce a VRP"
    print(f"\n(written to {OUT / 'vrp_e2e_result.json'})")


if __name__ == "__main__":
    main()
