"""Scoped Theta OPTIONS backfill puller — OPERATOR-ONLY, doubly gated.

Fixes the audit gaps that made the documented capture unexecutable:

- **G1**: the SWE tape puller cannot pull historical dates (it takes "the last
  ``--days`` business days from *today*" and applies ONE ``--expiration`` to all
  of them). This puller takes ``--start/--end`` and resolves the expiration
  **per session**: 0DTE when the session date is a listed expiration, else the
  nearest listed expiration on-or-after the session (never a dead contract).
- **G6**: trades come from ``/v3/option/history/trade_quote`` (trade + prevailing
  NBBO quote) so ``side_inferred`` is real, with ``/v3/option/history/trade`` as
  an explicit, honesty-preserving fallback (no NBBO columns -> every side is
  ``"mid"``, never guessed).
- **G11**: output lands under ``data_raw/theta/`` (this repo), never inside the
  read-only ``vendor/swe`` tree.

Design: pure transforms + an injected ``ThetaHTTPClient`` protocol. The real
HTTP client is constructed ONLY inside ``main()`` behind the same double gate as
``scripts.pull_theta_options_scoped`` (``--i-understand-this-pulls-live-theta``
AND env ``THETA_OPERATOR_CONFIRM=1``); without it, ``main`` prints a dry-run
plan and never opens a socket. The test suite exercises everything through fake
clients — the autouse network guard would fail the suite loudly otherwise.

Failure semantics: loud, per-partition. An HTTP error is a ``PullError`` recorded
in the run stats and the partition manifest — never a silently-empty parquet that
``--resume`` would later skip as complete (the SWE connector's silent-empty
behaviour is exactly what made endpoint support undiagnosable).

This module never stamps ``available_ts`` — raw capture stays raw; PIT stamping
is the ingest's job (``scripts.ingest_theta_options``).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from intraday.timeutils import trading_days  # noqa: E402

ET_ZONE = "America/New_York"

# SPX weeklies (incl. 0DTE) trade under root SPXW; the partition keeps the plain
# symbol while the Theta query uses the root. NOTE: prior SPXW captures carried
# junk OI (audit defect) — SPY/QQQ are the GEX spine; SPX is context only.
_THETA_QUERY_ROOTS: dict[str, str] = {"SPX": "SPXW"}

DEFAULT_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")


class PullError(RuntimeError):
    """A pull failed loudly (HTTP/network/parse) — recorded, never silently empty."""


@runtime_checkable
class ThetaHTTPClient(Protocol):
    """GET a Theta Terminal v3 path; return the CSV body parsed as a DataFrame.

    Tests inject fakes. The real client exists only behind the operator gate.
    """

    def get(self, path: str, params: Mapping[str, str]) -> pd.DataFrame: ...


class RealThetaClient:
    """Thin HTTP client for the local Theta Terminal (operator machines only).

    Raises :class:`PullError` on any network error or HTTP status >= 400 — a
    backfill must fail loudly per partition, not write empty parquet. An empty
    200 body is a legitimate "no data" and yields an empty frame.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:25503",
        *,
        read_timeout: float = 180.0,
        max_concurrent: int = 4,
    ) -> None:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self.base_url = base_url.rstrip("/")
        self.read_timeout = read_timeout
        self._sem = threading.Semaphore(max_concurrent)
        self._session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=4, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]
            )
        )
        self._session.mount("http://", adapter)

    def get(self, path: str, params: Mapping[str, str]) -> pd.DataFrame:
        import requests

        query = dict(params)
        query["format"] = "csv"
        url = f"{self.base_url}{path}"
        with self._sem:
            try:
                resp = self._session.get(url, params=query, timeout=(10.0, self.read_timeout))
            except requests.RequestException as e:  # pragma: no cover - operator-only
                raise PullError(f"network error GET {path} {dict(params)}: {e}") from e
        if resp.status_code >= 400:
            raise PullError(
                f"HTTP {resp.status_code} GET {path} {dict(params)}: {resp.text[:200]}"
            )
        body = resp.text.strip()
        if not body:
            return pd.DataFrame()
        return pd.read_csv(io.StringIO(body))


@dataclass(frozen=True)
class TapeScope:
    """The scoped backfill request (kept inside one Theta billing month)."""

    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    start: date = date(2022, 1, 3)            # the 0DTE era S1/S2 need
    end: date = date(2026, 6, 1)
    strikes_each_side: int = 10               # server-side strike_range filter
    cadence: str = "1m"                       # quote-bar interval
    trades_endpoint: str = "trade_quote"      # "trade_quote" | "trade"
    out_root: Path = Path("data_raw/theta")   # G11: never inside vendor/

    def __post_init__(self) -> None:
        # G11 is enforced, not assumed: vendor/ is a read-only dependency tree.
        if "vendor" in Path(self.out_root).resolve().parts:
            raise ValueError(
                f"out_root must never sit inside a vendor/ tree (read-only): "
                f"{self.out_root}"
            )


@dataclass
class SessionResult:
    """Normalized frames + capture metadata for one (symbol, session)."""

    expiration: date
    trades: pd.DataFrame
    quotes: pd.DataFrame
    oi: pd.DataFrame
    endpoints: dict[str, str]
    strike_divisor: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunStats:
    planned: int = 0
    pulled: int = 0
    skipped_resume: int = 0
    failed: list[tuple[str, str, str]] = field(default_factory=list)  # (sym, day, err)


# --------------------------------------------------------------------------- #
# Pure transforms (fully offline-testable)
# --------------------------------------------------------------------------- #
def resolve_expiry_for_day(expirations: pd.DatetimeIndex, day: date) -> date:
    """0DTE if ``day`` is listed, else the nearest listed expiration ON-OR-AFTER
    ``day`` — never a past (dead) contract. Raises :class:`PullError` when the
    listing carries nothing at-or-after ``day``."""
    if len(expirations) == 0:
        raise PullError(f"empty expiration listing; cannot resolve an expiry for {day}")
    days = pd.DatetimeIndex(expirations).normalize()
    target = pd.Timestamp(day)
    candidates = days[days >= target]
    if len(candidates) == 0:
        raise PullError(f"no listed expiration on-or-after {day}")
    return candidates.min().date()


def _first_present(raw: pd.DataFrame, names: tuple[str, ...]) -> str:
    for n in names:
        if n in raw.columns:
            return n
    raise PullError(f"none of {names} present in response columns {list(raw.columns)[:12]}")


def _to_utc(values: pd.Series) -> pd.Series:
    """Theta v3 timestamps are ET wall-clock ISO strings; numeric values are
    treated as epoch milliseconds (already UTC)."""
    if pd.api.types.is_numeric_dtype(values):
        return pd.to_datetime(values, unit="ms", utc=True)
    parsed = pd.to_datetime(values)
    if parsed.dt.tz is None:
        return parsed.dt.tz_localize(ET_ZONE).dt.tz_convert("UTC")
    return parsed.dt.tz_convert("UTC")


def _norm_right(values: pd.Series) -> pd.Series:
    return values.astype(str).str.strip().str.upper().str[0]


def _to_date(values: pd.Series) -> pd.Series:
    """Dates arrive as ISO strings or YYYYMMDD integers; never let an integer
    be misparsed as an epoch."""
    if pd.api.types.is_numeric_dtype(values):
        return pd.to_datetime(values.astype("Int64").astype(str), format="%Y%m%d").dt.date
    return pd.to_datetime(values).dt.date


def _strike_divisor(strikes: pd.Series) -> float:
    """Theta endpoints variously report strikes in dollars or in 1/1000 dollars.

    Heuristic (recorded in the manifest, surfaced by --probe-day): values are
    millis when far beyond any plausible dollar strike. SPX trades near 6,000
    real dollars, so the threshold sits well above index levels.
    """
    s = pd.to_numeric(strikes, errors="coerce").dropna()
    if len(s) and float(s.min()) >= 50_000.0:
        return 1000.0
    return 1.0


def _drop_opra_test_rows(frame: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    """OPRA emits test contracts with absurd expirations (e.g. year 1882)."""
    exp = pd.to_datetime(frame["expiration"], errors="coerce")
    bad = exp.isna() | (exp.dt.year < 2000)
    if bool(bad.any()):
        warnings.append(f"dropped {int(bad.sum())} rows with invalid expirations")
    return frame.loc[~bad]


def classify_side(price: float, bid: float, ask: float) -> str:
    """Aggressor side vs the prevailing quote (SWE semantics, re-implemented):

    - ``mid``  when the quote is missing or crossed (``ask <= bid``) — honest unknown;
    - ``buy``  at/above the ask, ``sell`` at/below the bid;
    - interior prints: ``buy`` strictly above mid, else ``sell`` (an exact mid
      print resolves to ``sell`` — the deliberate tie convention).
    """
    eps = 1e-9
    if not (np.isfinite(price) and np.isfinite(bid) and np.isfinite(ask)) or ask <= bid:
        return "mid"
    if price >= ask - eps:
        return "buy"
    if price <= bid + eps:
        return "sell"
    return "buy" if price > (bid + ask) / 2.0 else "sell"


_TRADE_TS_ALIASES = ("trade_timestamp", "ts", "timestamp", "trade_time", "created")
_QUOTE_TS_ALIASES = ("quote_timestamp", "ts", "timestamp", "bar_start", "created")
_PRICE_ALIASES = ("price", "trade_price", "last")
_SIZE_ALIASES = ("size", "trade_size", "volume")
_BID_ALIASES = ("bid", "nbbo_bid", "bid_price")
_ASK_ALIASES = ("ask", "nbbo_ask", "ask_price")


def _normalize_trades_common(
    raw: pd.DataFrame, *, has_nbbo: bool
) -> tuple[pd.DataFrame, list[str], float]:
    warnings: list[str] = []
    out = pd.DataFrame()
    out["ts"] = _to_utc(raw[_first_present(raw, _TRADE_TS_ALIASES)])
    out["expiration"] = _to_date(raw["expiration"])
    divisor = _strike_divisor(raw["strike"])
    out["strike"] = pd.to_numeric(raw["strike"], errors="coerce") / divisor
    out["right"] = _norm_right(raw["right"])
    out["price"] = pd.to_numeric(raw[_first_present(raw, _PRICE_ALIASES)], errors="coerce")
    out["size"] = pd.to_numeric(raw[_first_present(raw, _SIZE_ALIASES)], errors="coerce")
    if has_nbbo:
        out["nbbo_bid"] = pd.to_numeric(raw[_first_present(raw, _BID_ALIASES)], errors="coerce")
        out["nbbo_ask"] = pd.to_numeric(raw[_first_present(raw, _ASK_ALIASES)], errors="coerce")
        out["side_inferred"] = [
            classify_side(p, b, a)
            for p, b, a in zip(out["price"], out["nbbo_bid"], out["nbbo_ask"], strict=True)
        ]
    else:
        out["nbbo_bid"] = np.nan
        out["nbbo_ask"] = np.nan
        out["side_inferred"] = "mid"
        warnings.append("no NBBO columns in trade response; every side_inferred is 'mid'")
    out = _drop_opra_test_rows(out, warnings)
    return out.reset_index(drop=True), warnings, divisor


def normalize_trade_quote(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str], float]:
    """``/v3/option/history/trade_quote`` -> trades with real ``side_inferred``.

    The endpoint returns each trade with its PREVAILING quote (strictly before
    the trade by default), so the aggressor sign is PIT-safe by construction.
    """
    if raw.empty:
        return _empty_trades(), [], 1.0
    return _normalize_trades_common(raw, has_nbbo=True)


def normalize_trades_fallback(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str], float]:
    """``/v3/option/history/trade`` fallback: NBBO columns may be absent; every
    side then degrades to ``"mid"`` (honest unknown) rather than a guess."""
    if raw.empty:
        return _empty_trades(), [], 1.0
    has_nbbo = any(c in raw.columns for c in _BID_ALIASES) and any(
        c in raw.columns for c in _ASK_ALIASES
    )
    return _normalize_trades_common(raw, has_nbbo=has_nbbo)


def normalize_quotes(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str], float]:
    """``/v3/option/history/quote`` interval bars -> per-contract quote rows.

    ``mid`` is set ONLY when ``bid > 0`` and ``ask > bid``; one-sided or crossed
    quotes carry ``mid = NaN`` so downstream IV inversion / parity spot never
    consume junk."""
    if raw.empty:
        return _empty_quotes(), [], 1.0
    warnings: list[str] = []
    out = pd.DataFrame()
    out["ts"] = _to_utc(raw[_first_present(raw, _QUOTE_TS_ALIASES)])
    out["expiration"] = _to_date(raw["expiration"])
    divisor = _strike_divisor(raw["strike"])
    out["strike"] = pd.to_numeric(raw["strike"], errors="coerce") / divisor
    out["right"] = _norm_right(raw["right"])
    out["bid"] = pd.to_numeric(raw[_first_present(raw, _BID_ALIASES)], errors="coerce")
    out["ask"] = pd.to_numeric(raw[_first_present(raw, _ASK_ALIASES)], errors="coerce")
    for col, aliases in (("bid_size", ("bid_size",)), ("ask_size", ("ask_size",))):
        out[col] = (
            pd.to_numeric(raw[aliases[0]], errors="coerce") if aliases[0] in raw.columns else np.nan
        )
    valid = (out["bid"] > 0) & (out["ask"] > out["bid"])
    out["mid"] = np.where(valid, (out["bid"] + out["ask"]) / 2.0, np.nan)
    n_junk = int((~valid).sum())
    if n_junk:
        warnings.append(f"{n_junk} quote rows one-sided/crossed -> mid=NaN")
    out = _drop_opra_test_rows(out, warnings)
    return out.reset_index(drop=True), warnings, divisor


def normalize_oi(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str], float]:
    """``/v3/option/history/open_interest`` -> one OI row per contract.

    This is the PRIOR settlement's OI (daily EOD); the ingest enforces that a
    day-D snapshot only ever consumes a strictly-earlier session's file."""
    if raw.empty:
        return _empty_oi(), [], 1.0
    warnings: list[str] = []
    out = pd.DataFrame()
    date_col = _first_present(raw, ("date", "created", "timestamp"))
    out["date"] = _to_date(raw[date_col])
    out["expiration"] = _to_date(raw["expiration"])
    divisor = _strike_divisor(raw["strike"])
    out["strike"] = pd.to_numeric(raw["strike"], errors="coerce") / divisor
    out["right"] = _norm_right(raw["right"])
    out["open_interest"] = pd.to_numeric(
        raw[_first_present(raw, ("open_interest", "oi"))], errors="coerce"
    )
    out = _drop_opra_test_rows(out, warnings)
    return out.reset_index(drop=True), warnings, divisor


def _empty_trades() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["ts", "expiration", "strike", "right", "price", "size",
                 "nbbo_bid", "nbbo_ask", "side_inferred"]
    )


def _empty_quotes() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["ts", "expiration", "strike", "right", "bid", "ask",
                 "bid_size", "ask_size", "mid"]
    )


def _empty_oi() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", "expiration", "strike", "right", "open_interest"])


# --------------------------------------------------------------------------- #
# Pull orchestration (client injected; no socket in this layer)
# --------------------------------------------------------------------------- #
def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def fetch_expirations(client: ThetaHTTPClient, symbol: str) -> pd.DatetimeIndex:
    """One FREE listing call per symbol; ``resolve_expiry_for_day`` is pure
    against it (1 call/symbol instead of 1/day)."""
    root = _THETA_QUERY_ROOTS.get(symbol, symbol)
    raw = client.get("/v3/option/list/expirations", {"symbol": root})
    if raw.empty:
        raise PullError(f"no expirations listed for {symbol} (root {root})")
    col = "expiration" if "expiration" in raw.columns else raw.columns[0]
    return pd.DatetimeIndex(pd.to_datetime(raw[col])).sort_values()


def pull_session(
    client: ThetaHTTPClient,
    symbol: str,
    day: date,
    scope: TapeScope,
    *,
    expirations: pd.DatetimeIndex,
) -> SessionResult:
    """Pull one (symbol, session): per-day expiry, trades, quote bars, OI."""
    expiry = resolve_expiry_for_day(expirations, day)
    root = _THETA_QUERY_ROOTS.get(symbol, symbol)
    base = {
        "symbol": root,
        "expiration": _yyyymmdd(expiry),
        "date": _yyyymmdd(day),
        "strike_range": str(scope.strikes_each_side),
    }
    endpoints: dict[str, str] = {}
    warnings: list[str] = []

    if scope.trades_endpoint == "trade_quote":
        try:
            raw_trades = client.get("/v3/option/history/trade_quote", base)
            trades, w, div_t = normalize_trade_quote(raw_trades)
            endpoints["trades"] = "/v3/option/history/trade_quote"
        except PullError as e:
            warnings.append(f"trade_quote failed ({e}); fell back to /trade")
            raw_trades = client.get("/v3/option/history/trade", base)
            trades, w, div_t = normalize_trades_fallback(raw_trades)
            endpoints["trades"] = "/v3/option/history/trade"
    else:
        raw_trades = client.get("/v3/option/history/trade", base)
        trades, w, div_t = normalize_trades_fallback(raw_trades)
        endpoints["trades"] = "/v3/option/history/trade"
    warnings.extend(w)

    raw_quotes = client.get("/v3/option/history/quote", {**base, "interval": scope.cadence})
    quotes, w, div_q = normalize_quotes(raw_quotes)
    endpoints["quotes"] = "/v3/option/history/quote"
    warnings.extend(w)

    raw_oi = client.get(
        "/v3/option/history/open_interest",
        {"symbol": root, "expiration": _yyyymmdd(expiry), "date": _yyyymmdd(day)},
    )
    oi, w, div_o = normalize_oi(raw_oi)
    endpoints["oi"] = "/v3/option/history/open_interest"
    warnings.extend(w)

    # Strike-unit cross-check: only endpoints that returned DATA get a vote
    # (an empty frame's 1.0 is "no opinion", not "dollars"), and any genuine
    # disagreement — including millis-vs-dollars — is a loud per-session error.
    votes = {
        name: d
        for name, d, frame in (
            ("trades", div_t, trades), ("quotes", div_q, quotes), ("oi", div_o, oi)
        )
        if len(frame)
    }
    distinct = set(votes.values())
    if len(distinct) > 1:
        raise PullError(
            f"strike-unit mismatch across endpoints for {symbol} {day}: {votes}"
        )
    divisor = distinct.pop() if distinct else 1.0
    if symbol in _THETA_QUERY_ROOTS:
        warnings.append(
            f"queried root {root} for {symbol}; prior {root} captures had junk OI - "
            "validate before trusting GEX"
        )
    return SessionResult(
        expiration=expiry, trades=trades, quotes=quotes, oi=oi,
        endpoints=endpoints, strike_divisor=divisor, warnings=warnings,
    )


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    frame.to_parquet(tmp, engine="pyarrow", index=False)
    tmp.replace(path)


def write_session(out_root: Path, symbol: str, day: date, res: SessionResult) -> Path:
    """Atomic write of trades/quotes/oi parquets + ``_manifest.json``."""
    part = out_root / f"ticker={symbol.upper()}" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(res.trades, part / "trades.parquet")
    _atomic_write_parquet(res.quotes, part / "quotes.parquet")
    _atomic_write_parquet(res.oi, part / "oi.parquet")
    manifest = {
        "symbol": symbol.upper(),
        "date": day.isoformat(),
        "expiration": res.expiration.isoformat(),
        "endpoints": res.endpoints,
        "rows": {"trades": len(res.trades), "quotes": len(res.quotes), "oi": len(res.oi)},
        "strike_divisor": res.strike_divisor,
        "pulled_at_utc": datetime.now(UTC).isoformat(),
        "warnings": res.warnings,
        "complete": True,
    }
    (part / "_manifest.json").write_text(json.dumps(manifest, indent=1))
    return part


def _is_complete(out_root: Path, symbol: str, day: date) -> bool:
    mf = out_root / f"ticker={symbol.upper()}" / f"date={day.isoformat()}" / "_manifest.json"
    if not mf.exists():
        return False
    try:
        return bool(json.loads(mf.read_text()).get("complete", False))
    except (json.JSONDecodeError, OSError):
        return False


def run_pull(
    scope: TapeScope,
    client: ThetaHTTPClient,
    *,
    resume: bool = False,
    workers: int = 2,
    trading_days_fn: Callable[[date, date], list[date]] = trading_days,
    log: Callable[[str], None] = print,
) -> RunStats:
    """Execute the scoped pull. Failures are loud, per-partition, and recorded;
    the run continues so one bad session never aborts a billed backfill."""
    stats = RunStats()
    lock = threading.Lock()
    days = trading_days_fn(scope.start, scope.end)
    # A failed expiration listing kills only THAT symbol, is journaled like any
    # other failure, and the rest of the (billed) run proceeds.
    expirations: dict[str, pd.DatetimeIndex] = {}
    for sym in scope.symbols:
        try:
            expirations[sym] = fetch_expirations(client, sym)
        except PullError as e:
            stats.failed.append((sym, "<expirations>", str(e)))
            log(f"  {sym}: expiration listing FAILED - {e}")
    work = [(sym, d) for sym in scope.symbols if sym in expirations for d in days]
    stats.planned = len(work)

    def one(item: tuple[str, date]) -> None:
        sym, d = item
        if resume and _is_complete(scope.out_root, sym, d):
            with lock:
                stats.skipped_resume += 1
            return
        try:
            res = pull_session(client, sym, d, scope, expirations=expirations[sym])
            write_session(scope.out_root, sym, d, res)
            with lock:
                stats.pulled += 1
            log(f"  {sym} {d}: exp={res.expiration} trades={len(res.trades)} "
                f"quotes={len(res.quotes)} oi={len(res.oi)}")
        except PullError as e:
            with lock:
                stats.failed.append((sym, d.isoformat(), str(e)))
            log(f"  {sym} {d}: FAILED - {e}")

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        list(pool.map(one, work))

    runs_path = scope.out_root / "_runs.json"
    scope.out_root.mkdir(parents=True, exist_ok=True)
    prior = json.loads(runs_path.read_text()) if runs_path.exists() else []
    prior.append({
        "at_utc": datetime.now(UTC).isoformat(),
        "symbols": list(scope.symbols),
        "window": [scope.start.isoformat(), scope.end.isoformat()],
        "planned": stats.planned, "pulled": stats.pulled,
        "skipped_resume": stats.skipped_resume, "failed": stats.failed,
    })
    runs_path.write_text(json.dumps(prior, indent=1))
    return stats


# --------------------------------------------------------------------------- #
# CLI (doubly gated; dry-run by default)
# --------------------------------------------------------------------------- #
def _render_plan(scope: TapeScope, n_days: int) -> str:
    n = n_days * len(scope.symbols)
    spx_warning = (
        [
            "",
            "  WARNING: SPX/SPXW is TAPE-ONLY context. The ingest REFUSES to",
            "  synthesize SPX(W) chains (AM/PM settlement ambiguity + junk OI on",
            "  prior captures) - SPX tape can never feed GEX. Spend accordingly.",
        ]
        if any(s in ("SPX", "SPXW") for s in scope.symbols)
        else []
    )
    return "\n".join([
        "Scoped Theta TAPE backfill - DRY RUN (no socket opened).",
        f"  symbols={list(scope.symbols)} window={scope.start}..{scope.end} "
        f"({n_days} sessions, {n} session-pulls)",
        *spx_warning,
        f"  strikes=ATM+/-{scope.strikes_each_side} (server-side strike_range) "
        f"quote-cadence={scope.cadence} trades-endpoint={scope.trades_endpoint}",
        f"  out={scope.out_root} (manifest per partition; --resume skips complete ones)",
        "",
        "  Per session: 1x trade_quote (tick trades + prevailing NBBO),",
        f"  1x quote bars @ {scope.cadence}, 1x daily open_interest.",
        "  Expirations resolve PER SESSION: 0DTE when listed, else nearest on-or-after.",
        "",
        "To execute: BOTH --i-understand-this-pulls-live-theta AND",
        "env THETA_OPERATOR_CONFIRM=1 (operator only; see docs/OPERATOR_RUNBOOK.md).",
        "Start with --probe-day <recent session> to verify endpoint schemas/costs",
        "before any backfill.",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Scoped Theta options tape/quotes/OI backfill (operator-only, gated)."
    )
    p.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--start", type=date.fromisoformat, default=date(2022, 1, 3))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 6, 1))
    p.add_argument("--strikes-each-side", dest="strikes", type=int, default=10)
    p.add_argument("--cadence", default="1m", help="quote-bar interval")
    p.add_argument("--trades-endpoint", choices=["trade_quote", "trade"], default="trade_quote")
    p.add_argument("--out-root", type=Path, default=Path("data_raw/theta"))
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--resume", action="store_true",
                   help="skip partitions whose manifest says complete")
    p.add_argument("--base-url", default="http://127.0.0.1:25503")
    p.add_argument("--read-timeout", type=float, default=180.0)
    p.add_argument("--probe-day", type=date.fromisoformat, default=None,
                   help="pull exactly ONE session and report raw schemas/row counts")
    p.add_argument(
        "--i-understand-this-pulls-live-theta", dest="confirm_flag", action="store_true",
        help="operator confirmation flag (also needs env THETA_OPERATOR_CONFIRM=1)",
    )
    args = p.parse_args(argv)

    scope = TapeScope(
        symbols=tuple(s.upper() for s in args.symbols),
        start=args.start, end=args.end,
        strikes_each_side=args.strikes, cadence=args.cadence,
        trades_endpoint=args.trades_endpoint, out_root=args.out_root,
    )

    confirmed = args.confirm_flag and os.environ.get("THETA_OPERATOR_CONFIRM") == "1"
    if not confirmed:
        # No RealThetaClient is ever constructed on this path - no socket.
        print(_render_plan(scope, len(trading_days(scope.start, scope.end))))
        return 0

    client = RealThetaClient(  # pragma: no cover - operator-only from here on
        args.base_url, read_timeout=args.read_timeout, max_concurrent=4
    )
    if args.probe_day is not None:  # pragma: no cover - operator-only
        probe_scope = TapeScope(
            symbols=scope.symbols, start=args.probe_day, end=args.probe_day,
            strikes_each_side=scope.strikes_each_side, cadence=scope.cadence,
            trades_endpoint=scope.trades_endpoint, out_root=scope.out_root,
        )
        stats = run_pull(probe_scope, client, workers=1)
        print(f"probe: pulled={stats.pulled} failed={stats.failed}")
        print("Inspect the partition manifests (rows, warnings, strike_divisor) "
              "before running the backfill.")
        return 0 if not stats.failed else 1

    stats = run_pull(  # pragma: no cover - operator-only
        scope, client, resume=args.resume, workers=args.workers
    )
    print(f"pulled={stats.pulled} skipped={stats.skipped_resume} failed={len(stats.failed)}")
    return 0 if not stats.failed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
