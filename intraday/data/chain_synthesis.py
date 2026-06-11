"""PIT-safe option-chain synthesis from quote mids + daily open interest.

The pure core of the Theta ingest (``scripts.ingest_theta_options`` is the thin
CLI over it, mirroring the :mod:`intraday.data.ibkr` layering): per-contract
last-quote mids at each snapshot, locally Black-Scholes-inverted ``implied_vol``,
put-call-parity ``spot``, prior-session OI — emitted as ``CHAIN_COLUMNS`` rows.

Binding correctness rules (the adversarial PIT/math spec; each enforced by test):

- **PIT (R1/R2/R3)**: a snapshot consumes only quotes at-or-before its own
  ``snapshot_ts`` (and at most one cadence step old — staler is dropped); the OI
  it carries must be a strictly-earlier session's settlement number, constant
  across the day; ``available_ts = snapshot_ts + chain_latency``.
- **Inversion clock (R7)**: ``implied_vol`` is inverted at the SAME ``T`` its
  consumer re-prices with — the SWE dealer analyzer's ``max(days, 1)/365``
  (1/365 for 0DTE all day), NOT an intraday-decaying T. Mixing clocks makes the
  stored IV unable to reproduce the observed premium, with the error growing
  toward the close. ``parity.year_fraction`` (decaying) is used ONLY for the
  parity spot, exactly as its consumer does.
- **(r, q) consistency (R8)**: ``r = config.gate.risk_free_rate`` and the
  per-symbol ``q`` below feed BOTH the parity spot and the IV inversion. Known
  residual: the GEX consumer (``features.gex`` → SWE dealer analyzer) re-prices
  greeks at ``q = 0``; the resulting gamma error is < 0.04% for SPY/QQQ at the
  tenors exercised (0DTE–1 month), measured in review. Plumbing q through the
  analyzer is a noted follow-up, not a correctness blocker.
- **Honest absence (R5/R6/R9)**: only quotes with ``bid > 0``, ``ask > bid`` and
  a sane relative spread invert; failed or junk (>= 500%) roots are DROPPED and
  counted, never placeholdered; a snapshot with no usable parity pair is dropped
  whole. Missing OI becomes 0 (no gamma weight) — but if an OI table was
  supplied and EVERY contract misses it, that is a systematic key mismatch and
  the synthesis raises instead of silently zeroing all gamma.
- **Same-day expiry (R13/G8)**: the engine hardcodes ``expiry == session day``;
  :func:`validate_same_day_expiry` reports the split so callers can refuse.
- **SPY/QQQ only (R14)**: SPX/SPXW synthesis is refused (AM/PM settlement
  ambiguity + junk SPXW OI on prior captures).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..config import SessionConfig
from ..contracts import CHAIN_COLUMNS
from ..timeutils import bar_close_index, parse_interval
from .parity import implied_spot, year_fraction

# Per-symbol continuous dividend yields (parity.py's documented figures); one
# (r, q) pair feeds parity spot AND the IV inversion (R8).
PER_SYMBOL_Q: dict[str, float] = {"SPY": 0.013, "QQQ": 0.006}

# The GEX spine. SPX/SPXW synthesis is refused (R14).
REFUSED_ROOTS = frozenset({"SPX", "SPXW"})

# Junk-root ceiling: the vendor solver's Brent fallback brackets [0.001, 10];
# a deep wing with ~zero vega can "converge" to an absurd root (R6). `>=` keeps
# the boundary consistent with the SWE dealer analyzer's strict `iv < 5.0` keep.
MAX_PLAUSIBLE_IV = 5.0

# OI join keys round strikes to a fixed grid so a one-ULP representation drift
# between the quote and OI endpoints cannot silently zero every gamma weight.
_STRIKE_KEY_DECIMALS = 3


def consumer_T(expiry: date, day: date) -> float:
    """The exact T the downstream GEX consumer re-prices with (R7):
    SWE ``dealer_positioning.analyze`` uses ``max((expiry - day).days, 1)/365``,
    pinned at 1/365 for a same-day expiry all session long."""
    return max((pd.Timestamp(expiry) - pd.Timestamp(day)).days, 1) / 365.0


def _strike_key(strike: float) -> float:
    return round(float(strike), _STRIKE_KEY_DECIMALS)


@dataclass
class SynthStats:
    """Per-day synthesis accounting (printed and persisted to the sidecar)."""

    snapshots_total: int = 0
    snapshots_no_spot: int = 0
    rows_emitted: int = 0
    rows_dropped_iv: int = 0
    oi_missing_contracts: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "snapshots_total": self.snapshots_total,
            "snapshots_no_spot": self.snapshots_no_spot,
            "rows_emitted": self.rows_emitted,
            "rows_dropped_iv": self.rows_dropped_iv,
            "oi_missing_contracts": self.oi_missing_contracts,
            "warnings": self.warnings,
        }


def parity_spot_at(
    last_quotes: pd.DataFrame,
    *,
    snapshot_ts: pd.Timestamp,
    r: float,
    q: float,
    session: SessionConfig,
    max_leg_rel_spread: float = 0.25,
) -> float | None:
    """Spot via put-call parity from the snapshot's same-expiry C/P mid pairs.

    Median of ``implied_spot`` over the 3 strikes with the smallest
    ``|call_mid - put_mid|`` (nearest-forward; the median kills single-strike
    quote glitches). A leg whose relative spread exceeds ``max_leg_rel_spread``
    is rejected as stale/illiquid (R12). Returns ``None`` (refusal, R11) when no
    usable pair exists — never an approximation.

    Uses ``parity.year_fraction`` (decaying, anchored at the expiry's RTH close)
    — correct for SPOT only; the IV inversion deliberately uses a different,
    consumer-matched T (R7).
    """
    lq = last_quotes
    ok = (
        (lq["bid"] > 0) & (lq["ask"] > lq["bid"]) & lq["mid"].notna()
        & ((lq["ask"] - lq["bid"]) / lq["mid"] <= max_leg_rel_spread)
    )
    lq = lq.loc[ok]
    if lq.empty:
        return None
    calls = lq[lq["right"] == "C"].set_index(["expiration", "strike"])["mid"]
    puts = lq[lq["right"] == "P"].set_index(["expiration", "strike"])["mid"]
    pairs = pd.DataFrame({"call_mid": calls, "put_mid": puts}).dropna()
    if pairs.empty:
        return None
    pairs = pairs.assign(gap=(pairs["call_mid"] - pairs["put_mid"]).abs())
    pairs = pairs.sort_values("gap").head(3)
    spots = [
        float(
            implied_spot(
                row["call_mid"], row["put_mid"], strike,
                r=r, q=q, T=year_fraction(snapshot_ts, expiration, session=session),
            )
        )
        for (expiration, strike), row in pairs.iterrows()
    ]
    return float(np.median(spots))


def synthesize_chain(
    quotes: pd.DataFrame,
    oi: pd.DataFrame,
    *,
    symbol: str,
    day: date,
    cadence: str = "5m",
    r: float,
    q: float = 0.0,
    session: SessionConfig | None = None,
    latency: pd.Timedelta = pd.Timedelta(milliseconds=1_000),
    max_leg_rel_spread: float = 0.25,
) -> tuple[pd.DataFrame, SynthStats]:
    """1m quote mids + daily OI -> CHAIN_COLUMNS snapshots on the cadence grid.

    See the module docstring for the binding rules this implements.
    """
    sym = symbol.upper()
    if sym in REFUSED_ROOTS:
        raise ValueError(
            f"refusing to synthesize a chain for {sym}: AM/PM settlement ambiguity "
            "and junk SPXW OI on prior captures (R14). The GEX spine is SPY/QQQ."
        )
    # Vendor solver imported lazily (the gex.py pattern): keep intraday.data
    # importable without vendor/swe on the path until synthesis actually runs.
    from engine.option_pricer import implied_volatility  # vendor/swe, read-only

    session = session or SessionConfig()
    stats = SynthStats()
    grid = bar_close_index(day, cadence, session)
    stats.snapshots_total = len(grid)
    max_age = parse_interval(cadence)

    qf = quotes.copy()
    qf["ts"] = pd.to_datetime(qf["ts"], utc=True)
    qf = qf.sort_values("ts")

    oi_map: dict[tuple[date, float, str], float] = {}
    for _, row in oi.iterrows():
        key = (pd.Timestamp(row["expiration"]).date(), _strike_key(row["strike"]),
               str(row["right"]))
        oi_map[key] = float(row["open_interest"])

    rows: list[dict] = []
    seen_contracts: set[tuple[date, float, str]] = set()
    missing_oi_keys: set[tuple[date, float, str]] = set()
    for snap in grid:
        # Staleness rule (R12) is the window itself: a quote older than one
        # cadence step from the snapshot simply never enters it.
        window = qf[(qf["ts"] <= snap) & (qf["ts"] > snap - max_age)]
        if window.empty:
            stats.snapshots_no_spot += 1
            continue
        last = (
            window.groupby(["expiration", "strike", "right"], as_index=False)
            .last()
        )
        spot = parity_spot_at(
            last, snapshot_ts=snap, r=r, q=q, session=session,
            max_leg_rel_spread=max_leg_rel_spread,
        )
        if spot is None or not np.isfinite(spot) or spot <= 0:
            stats.snapshots_no_spot += 1
            continue
        for _, lrow in last.iterrows():
            mid = lrow["mid"]
            if not (np.isfinite(mid) if mid is not None else False) or mid <= 0:
                stats.rows_dropped_iv += 1
                continue
            expiration = pd.Timestamp(lrow["expiration"]).date()
            strike = float(lrow["strike"])
            right = str(lrow["right"])
            iv = implied_volatility(
                float(mid), spot, strike, consumer_T(expiration, day), r,
                "call" if right == "C" else "put", q=q,
            )
            if iv is None or not np.isfinite(iv) or iv <= 0 or iv >= MAX_PLAUSIBLE_IV:
                stats.rows_dropped_iv += 1
                continue
            key = (expiration, _strike_key(strike), right)
            seen_contracts.add(key)
            oi_val = oi_map.get(key)
            if oi_val is None:
                missing_oi_keys.add(key)
                oi_val = 0.0
            rows.append({
                "snapshot_ts": snap,
                "available_ts": snap + latency,
                "expiration": expiration,
                "strike": strike,
                "option_type": right,
                "open_interest": oi_val,
                "implied_vol": float(iv),
                "spot": spot,
            })
    stats.rows_emitted = len(rows)
    stats.oi_missing_contracts = len(missing_oi_keys)
    if missing_oi_keys:
        if len(oi_map) > 0 and missing_oi_keys >= seen_contracts:
            # An OI table exists yet NOT ONE emitted contract matched it: that is
            # a systematic key-format mismatch (units/expiry parsing), not a few
            # genuinely-missing wings. Zeroing every gamma weight would silently
            # neuter GEX — refuse instead.
            raise ValueError(
                f"OI join matched 0 of {len(seen_contracts)} contracts against a "
                f"non-empty OI table ({len(oi_map)} keys) for {sym} {day} - "
                "systematic strike/expiry key mismatch; refusing to zero all gamma."
            )
        stats.warnings.append(
            f"{len(missing_oi_keys)} contracts had no prior-settlement OI -> 0 "
            "(no gamma weight; never a guess)"
        )
    frame = pd.DataFrame(rows, columns=list(CHAIN_COLUMNS))
    return frame, stats


def validate_same_day_expiry(frame: pd.DataFrame, day: date) -> tuple[int, int]:
    """G8/R13: the engine reads option features with ``expiry == session day``
    hardcoded; a chain without same-day rows yields all-None option features."""
    if frame.empty:
        return 0, 0
    exp = pd.to_datetime(frame["expiration"]).dt.date
    same = int((exp == day).sum())
    return same, int(len(frame) - same)


def quotes_to_parity_frame(quotes: pd.DataFrame) -> pd.DataFrame:
    """Per-timestamp nearest-forward ATM C/P pair -> ``reconstruct_spot`` input.

    Duplicate (ts, expiration, strike, right) rows resolve to the LAST quote —
    the same discipline as the chain path (a first-wins alignment would let a
    stale duplicate perturb the reconstruction).
    """
    qf = quotes.copy()
    qf["ts"] = pd.to_datetime(qf["ts"], utc=True)
    ok = (qf["bid"] > 0) & (qf["ask"] > qf["bid"]) & qf["mid"].notna()
    qf = qf.loc[ok].sort_values("ts")
    if qf.empty:
        return pd.DataFrame(columns=["strike", "call_mid", "put_mid", "expiry"])
    qf = qf.groupby(["ts", "expiration", "strike", "right"], as_index=False).last()
    calls = qf[qf["right"] == "C"].set_index(["ts", "expiration", "strike"])["mid"]
    puts = qf[qf["right"] == "P"].set_index(["ts", "expiration", "strike"])["mid"]
    pairs = pd.DataFrame({"call_mid": calls, "put_mid": puts}).dropna().reset_index()
    if pairs.empty:
        return pd.DataFrame(columns=["strike", "call_mid", "put_mid", "expiry"])
    pairs["gap"] = (pairs["call_mid"] - pairs["put_mid"]).abs()
    best = pairs.loc[pairs.groupby("ts")["gap"].idxmin()]
    out = best.rename(columns={"expiration": "expiry"}).set_index("ts")
    return out[["strike", "call_mid", "put_mid", "expiry"]]
