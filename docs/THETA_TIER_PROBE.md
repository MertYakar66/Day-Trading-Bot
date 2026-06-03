# Theta capability probe (P0)

> **Run once on 2026-06-01, before the "don't touch Theta this session"
> instruction arrived. NOT repeated.** Recorded here as the ground truth for what
> the real-data path can and cannot do. Do not re-probe while the operator is
> using the subscription concurrently.

> **SUPERSEDED / SCOPED — read [`REAL_DATA.md`](REAL_DATA.md) §1 for the corrected
> data tiers.** The raw probe findings below are still accurate *for Theta itself*,
> but the old conclusion "real intraday data is unavailable" is NOT how this engine
> sources data. The corrected path takes the **underlying** from **IBKR / Yahoo /
> put-call parity** (never Theta) and uses **Theta STANDARD for OPTIONS only**, so
> Theta's FREE-tier stock/index limits do not block the real-data path.

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

From **Theta** specifically: 1-second SPX/VIX, the intraday stock tape, option-chain
snapshots, and the option tape are **gated behind STANDARD/VALUE**; FREE tier serves
only EOD stock OHLCV. So Theta cannot serve intraday data at FREE tier.

**This does NOT block the engine** (see [`REAL_DATA.md`](REAL_DATA.md)). The corrected
real-data path sources the **underlying** intraday from **IBKR** or free **Yahoo**
bars, or from **put-call parity** for deep history — *never Theta* — and uses **Theta
only for OPTIONS, at STANDARD**. Build/validation runs against the deterministic
`SyntheticDataProvider`; the real options backfill awaits the operator's scoped
STANDARD pull ([`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md)).

The Theta adapter (`intraday/data/theta_adapter.py`, wired to SWE
`engine/theta_connector.py`) never opens a socket this session. Its actual behaviour
(corrected — there is no blanket `TierUnavailable`): `get_bars` raises
`DataUnavailable` (explicitly "not a tier error" — Theta is not the underlying
source), and the option methods raise `ThetaNotConnectedThisSession` unless the
adapter is constructed with `allow_connect=True`. (`TierUnavailable` exists as a
`DataUnavailable` subclass used by the provider-fallback chain, not raised here.)

## To activate the real path later

1. Upgrade the Theta subscription to **STANDARD** (real-time tick options + IV +
   1st-order Greeks, 1-min/tick stock, 1-second SPX/VIX).
2. Re-run a capability probe to confirm the unlocked endpoints.
3. Construct `ThetaDataProvider(allow_connect=True)` on the laptop with the
   Terminal up. The adapter implements the same `DataProvider` interface as the
   synthetic provider, so it is a drop-in; only `theta_adapter.py` needs the real
   fetch logic filled in against the (now-unlocked) endpoints.
