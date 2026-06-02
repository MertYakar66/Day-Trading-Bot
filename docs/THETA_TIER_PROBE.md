# Theta capability probe (P0)

> **Run once on 2026-06-01, before the "don't touch Theta this session"
> instruction arrived. NOT repeated.** Recorded here as the ground truth for what
> the real-data path can and cannot do. Do not re-probe while the operator is
> using the subscription concurrently.

## Result: subscription tier = **FREE**

Endpoints probed at `http://127.0.0.1:25503` (Theta v3; note v3 uses
`symbol=`, not the deprecated `root=`):

| Endpoint | Result |
|---|---|
| `/v3/stock/history/eod?symbol=SPY` | ✅ **works** — daily OHLCV + close NBBO snapshot |
| `/v3/index/snapshot/price?symbol=SPX` / `VIX` | ❌ requires STANDARD |
| `/v3/stock/snapshot/quote?symbol=SPY` (real-time) | ❌ requires VALUE |
| `/v3/stock/history/trade` (stock tape) | ❌ requires STANDARD |
| `/v3/stock/history/ohlc` (intraday stock bars) | ❌ empty / not on FREE |
| `/v3/option/list/expirations`, `/strikes` | ❌ empty (no option data on FREE) |
| `/v3/option/snapshot/greeks/first_order` | ❌ option data not on FREE |
| `/v3/option/history/eod` | ❌ "No data found" |

## Implication for this project

The intraday engine needs 1-second SPX/VIX, 1-minute SPY/QQQ + tick tape, option
chain snapshots, and the option tape — **all gated behind STANDARD/VALUE**. At
FREE tier only EOD stock OHLCV is available, which is far below intraday
requirements.

**Therefore: real intraday data is unavailable, and the engine is built and
validated entirely against the deterministic `SyntheticDataProvider`.** The
real-data path exists as code (`intraday/data/theta_adapter.py`, wired to SWE
`engine/theta_connector.py`) and raises a clear `TierUnavailable` for
intraday/option requests; it never opens a socket this session.

## To activate the real path later

1. Upgrade the Theta subscription to **STANDARD** (real-time tick options + IV +
   1st-order Greeks, 1-min/tick stock, 1-second SPX/VIX).
2. Re-run a capability probe to confirm the unlocked endpoints.
3. Construct `ThetaDataProvider(allow_connect=True)` on the laptop with the
   Terminal up. The adapter implements the same `DataProvider` interface as the
   synthetic provider, so it is a drop-in; only `theta_adapter.py` needs the real
   fetch logic filled in against the (now-unlocked) endpoints.
