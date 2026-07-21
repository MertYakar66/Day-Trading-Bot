"""Shared derivation core for the intraday OPTION pull (Bloomberg lab 2026-07-03).

Pure functions (no Bloomberg calls) -> unit-testable:
  - European Black-Scholes price + implied-vol inversion (bisection; guards at 0DTE / tiny-vega)
  - Lee-Ready trade-side classification (quote rule vs mid, tick-test on ties) -> {buy,sell,mid}
  - trade_nbbo: NBBO attached from the last quote STRICTLY BEFORE each trade (PIT, no look-ahead)
  - RTH minute grid + PIT spot alignment + time-to-expiry, all UTC / EDT-correct
  - symbol config (SPY/QQQ equity options; SPX via SPXW index options) + ticker construction

Conventions match the engine (intraday/contracts.py, config.py): r=0.04 flat (decimal), q=0 (engine
BS ignores dividends -> IV round-trip consistent with the pricer). option_type/right in {C,P};
side_inferred in {buy,sell,mid}, buy = customer buy-initiated (= dealer sells). UTC timestamps.
"""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

ET, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")
R_FLAT = 0.04
Q_FLAT = 0.0
YEAR_SECONDS = 365.0 * 24 * 3600.0
EXPIRY_CLOSE_ET = (16, 0)          # PM settlement (SPY/QQQ options, SPXW weeklies)

# root = Bloomberg option root; yk = yellow key; inc = strike increment; bars = bars_1m file key;
# exercise = American (SPY/QQQ) vs European (SPX) -> IV caveat (we invert with European BS for all).
SYMCFG = {
    "SPY": {"root": "SPY",  "yk": "Equity", "inc": 1.0, "bars": "SPY", "exercise": "American"},
    "QQQ": {"root": "QQQ",  "yk": "Equity", "inc": 1.0, "bars": "QQQ", "exercise": "American"},
    "SPX": {"root": "SPXW", "yk": "Index",  "inc": 5.0, "bars": "SPX", "exercise": "European"},
}


def native(x):
    return x.to_native() if hasattr(x, "to_native") else x


# ----------------------------- Black-Scholes / IV -----------------------------
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, t, sigma, right, r=R_FLAT, q=Q_FLAT):
    if spot <= 0 or strike <= 0:
        return float("nan")
    if t <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    dr, dq = math.exp(-r * t), math.exp(-q * t)
    if right == "C":
        return spot * dq * _norm_cdf(d1) - strike * dr * _norm_cdf(d2)
    return strike * dr * _norm_cdf(-d2) - spot * dq * _norm_cdf(-d1)


def implied_vol(price, spot, strike, t, right, r=R_FLAT, q=Q_FLAT,
                lo=1e-4, hi=5.0, tol=1e-7, iters=100, tv_floor=5e-3):
    """Invert BS for sigma by bisection. NaN when time value is below the floor (deep ITM / at
    expiry: vega ~0, IV indeterminate), when price <= discounted intrinsic, or above max-vol price.
    Never emits a garbage extreme-IV row."""
    if price is None or not np.isfinite(price) or price <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return float("nan")
    disc_intr = (max(spot * math.exp(-q * t) - strike * math.exp(-r * t), 0.0) if right == "C"
                 else max(strike * math.exp(-r * t) - spot * math.exp(-q * t), 0.0))
    if price - disc_intr < tv_floor:
        return float("nan")
    f_lo = bs_price(spot, strike, t, lo, right, r, q) - price
    f_hi = bs_price(spot, strike, t, hi, right, r, q) - price
    if f_lo >= 0 or f_hi <= 0:
        return float("nan")
    a, b = lo, hi
    for _ in range(iters):
        m = 0.5 * (a + b)
        fm = bs_price(spot, strike, t, m, right, r, q) - price
        if abs(fm) < tol:
            return m
        if fm > 0:
            b = m
        else:
            a = m
    return 0.5 * (a + b)


# ----------------------------- time / RTH grid / PIT --------------------------
def rth_utc_bounds(dstr):
    y, m, d = map(int, dstr.split("-"))
    o = datetime(y, m, d, 9, 30, tzinfo=ET).astimezone(UTC)
    c = datetime(y, m, d, 16, 0, tzinfo=ET).astimezone(UTC)
    return o, c


def rth_minute_grid(dstr):
    """Start-labelled UTC minute timestamps 09:30..15:59 ET (390 minutes)."""
    o, c = rth_utc_bounds(dstr)
    return pd.date_range(o, c - timedelta(minutes=1), freq="1min", tz="UTC")


def expiry_close_utc(exp_date_str):
    y, m, d = map(int, exp_date_str.split("-"))
    return datetime(y, m, d, EXPIRY_CLOSE_ET[0], EXPIRY_CLOSE_ET[1], tzinfo=ET).astimezone(UTC)


def years_to_expiry(snapshot_ts_utc, exp_date_str):
    dt = (expiry_close_utc(exp_date_str) - snapshot_ts_utc).total_seconds()
    return max(dt, 0.0) / YEAR_SECONDS


def pit_spot_series(bars_day, dstr):
    """PIT-correct per-minute spot on the RTH grid, NO look-ahead. bars_1m are START-labelled, so
    bar[t].close is the price at ~t+1min (future). Spot at snapshot t = close of the bar ENDING at
    t = bar[t-1min].close. For the 09:30 snapshot (no prior bar) use bar[09:30].open.
    bars_day: DataFrame indexed by (start-labelled) UTC ts with columns open, close."""
    grid = rth_minute_grid(dstr)
    if bars_day is None or not len(bars_day):
        return None
    shifted = bars_day["close"].copy()
    shifted.index = shifted.index + timedelta(minutes=1)   # bar[t].close -> PIT spot for snapshot t+1
    spot = shifted.reindex(grid)
    o0 = bars_day["open"].iloc[0]
    if pd.isna(spot.iloc[0]):
        spot.iloc[0] = o0                                  # 09:30 snapshot = the open print
    return spot.ffill()


# ----------------------------- NBBO / Lee-Ready (PIT) -------------------------
def trade_nbbo(ticks):
    """Attach to each TRADE the NBBO from the last quote STRICTLY BEFORE the trade ts (no
    concurrent/after quote -> no look-ahead). ticks: DataFrame[ts(UTC),type,value,size].
    Returns DataFrame[ts, price, size, nbbo_bid, nbbo_ask] in time order (may be empty)."""
    d = ticks.sort_values("ts", kind="stable").reset_index(drop=True)
    d["_bid"] = d["value"].where(d["type"] == "BID").ffill()
    d["_ask"] = d["value"].where(d["type"] == "ASK").ffill()
    q = d[["ts", "_bid", "_ask"]].dropna(subset=["_bid", "_ask"]).drop_duplicates("ts", keep="last")
    tr = (d[d["type"] == "TRADE"][["ts", "value", "size"]]
          .rename(columns={"value": "price"}).sort_values("ts").reset_index(drop=True))
    if not len(tr):
        return tr.assign(nbbo_bid=[], nbbo_ask=[])
    if not len(q):
        tr["nbbo_bid"] = np.nan; tr["nbbo_ask"] = np.nan
        return tr
    m = pd.merge_asof(tr, q, on="ts", direction="backward", allow_exact_matches=False)
    return m.rename(columns={"_bid": "nbbo_bid", "_ask": "nbbo_ask"})[
        ["ts", "price", "size", "nbbo_bid", "nbbo_ask"]]


def lee_ready(trades):
    """trades: DataFrame with price, nbbo_bid, nbbo_ask (time-sorted). Returns np.array of
    {buy,sell,mid}: quote rule vs mid, tick-test on exact-mid ties (or when NBBO absent)."""
    n = len(trades)
    price = trades["price"].to_numpy(dtype=float)
    bid = trades["nbbo_bid"].to_numpy(dtype=float)
    ask = trades["nbbo_ask"].to_numpy(dtype=float)
    mid = (bid + ask) / 2.0
    side = np.empty(n, dtype=object)
    last_diff_price, last_side, eps = np.nan, "mid", 1e-9
    for i in range(n):
        p, mi = price[i], mid[i]
        if np.isfinite(mi) and p > mi + eps:
            s = "buy"
        elif np.isfinite(mi) and p < mi - eps:
            s = "sell"
        else:                                   # exact mid or no NBBO -> tick test
            if np.isfinite(last_diff_price):
                s = "buy" if p > last_diff_price + eps else "sell" if p < last_diff_price - eps else last_side
            else:
                s = "mid"
        side[i] = s
        if s in ("buy", "sell"):
            last_side = s
        if not np.isfinite(last_diff_price) or abs(p - last_diff_price) > eps:
            last_diff_price = p
    return side


# ----------------------------- chain mid from bdib bid/ask bars ---------------
def bar_close_series(bars, dstr, grid=None):
    """Per-minute close on the RTH grid from a bdib frame (cols time, close). Reindexed to grid,
    NOT ffilled (gaps => NaN -> honest missing minutes)."""
    if grid is None:
        grid = rth_minute_grid(dstr)
    if bars is None or not (hasattr(bars, "columns") and len(bars)):
        return pd.Series(index=grid, dtype=float)
    b = bars.rename(columns={"time": "ts"}) if "time" in bars.columns else bars
    s = pd.Series(b["close"].values, index=pd.to_datetime(b["ts"], utc=True))
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(grid)


# ----------------------------- ticker construction ----------------------------
def atm_strike(open_spot, inc):
    return round(open_spot / inc) * inc


def opt_ticker(cfg, exp_date_str, strike, right):
    """Bloomberg option ticker, e.g. 'SPY US 07/02/26 C745 Equity' / 'SPXW US 07/02/26 C7485 Index'."""
    y, m, d = exp_date_str.split("-")
    mmddyy = f"{int(m):02d}/{int(d):02d}/{y[2:]}"
    ks = str(int(strike)) if float(strike).is_integer() else f"{strike:g}"
    return f"{cfg['root']} US {mmddyy} {right}{ks} {cfg['yk']}"
