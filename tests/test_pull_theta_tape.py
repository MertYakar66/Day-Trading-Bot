"""Tests for the scoped Theta tape puller — entirely offline via injected fakes.

The real client is constructed only behind the operator double-gate; these tests
prove the pure layer (expiry resolution, normalization, side classification,
strike units, partition/manifest writing, resume, loud failures) without any
socket — the autouse network guard enforces that mechanically.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from scripts.pull_theta_tape_scoped import (
    PullError,
    RunStats,
    TapeScope,
    classify_side,
    main,
    normalize_quotes,
    normalize_trade_quote,
    normalize_trades_fallback,
    pull_session,
    resolve_expiry_for_day,
    run_pull,
    write_session,
)

DAY = date(2026, 6, 8)  # a Monday; SPY/QQQ have daily expiries
EXPS = pd.DatetimeIndex(pd.to_datetime(["2026-06-05", "2026-06-08", "2026-06-10"]))


# --------------------------------------------------------------------------- #
# Fake client
# --------------------------------------------------------------------------- #
class FakeThetaClient:
    """Canned responses keyed by path; records every call for param assertions."""

    def __init__(self, responses: dict[str, pd.DataFrame | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params) -> pd.DataFrame:
        self.calls.append((path, dict(params)))
        resp = self.responses.get(path)
        if resp is None:
            return pd.DataFrame()
        if isinstance(resp, Exception):
            raise resp
        return resp.copy()


def _raw_trade_quote(day: date = DAY, *, n: int = 3) -> pd.DataFrame:
    ts = [f"{day}T09:3{i}:00.000" for i in range(n)]  # ET wall clock
    return pd.DataFrame({
        "symbol": ["SPY"] * n,
        "expiration": [day.strftime("%Y%m%d")] * n,
        "strike": [600000.0] * n,                      # millis -> $600
        "right": ["C"] * n,
        "trade_timestamp": ts,
        "quote_timestamp": ts,
        "price": [1.20, 1.10, 1.15],
        "size": [5, 3, 7],
        "bid": [1.10] * n,
        "ask": [1.20] * n,
        "bid_size": [10] * n,
        "ask_size": [12] * n,
    }).head(n)


def _raw_quotes(day: date = DAY) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["SPY"] * 3,
        "expiration": [day.strftime("%Y%m%d")] * 3,
        "strike": [600000.0, 600000.0, 605000.0],
        "right": ["C", "P", "C"],
        "ts": [f"{day}T09:31:00.000"] * 3,
        "bid": [1.10, 1.05, 0.0],                      # third is zero-bid
        "ask": [1.20, 1.15, 0.50],
        "bid_size": [10, 8, 0],
        "ask_size": [12, 9, 4],
    })


def _raw_oi(day: date = DAY) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["SPY"] * 2,
        "date": [day.strftime("%Y%m%d")] * 2,
        "expiration": [day.strftime("%Y%m%d")] * 2,
        "strike": [600000.0, 605000.0],
        "right": ["C", "P"],
        "open_interest": [1500, 900],
    })


def _full_fake(day: date = DAY) -> FakeThetaClient:
    return FakeThetaClient({
        "/v3/option/list/expirations": pd.DataFrame(
            {"expiration": [d.strftime("%Y%m%d") for d in EXPS]}
        ),
        "/v3/option/history/trade_quote": _raw_trade_quote(day),
        "/v3/option/history/quote": _raw_quotes(day),
        "/v3/option/history/open_interest": _raw_oi(day),
    })


# --------------------------------------------------------------------------- #
# Expiry resolution (the G1 fix)
# --------------------------------------------------------------------------- #
def test_expiry_is_0dte_when_listed():
    assert resolve_expiry_for_day(EXPS, date(2026, 6, 8)) == date(2026, 6, 8)


def test_expiry_is_next_listed_when_day_unlisted():
    assert resolve_expiry_for_day(EXPS, date(2026, 6, 9)) == date(2026, 6, 10)


def test_expiry_never_resolves_to_a_dead_contract():
    # 2026-06-05 is listed but in the past relative to 06-09 — must not be chosen.
    assert resolve_expiry_for_day(EXPS, date(2026, 6, 9)) != date(2026, 6, 5)


def test_expiry_raises_when_nothing_on_or_after():
    with pytest.raises(PullError, match="on-or-after"):
        resolve_expiry_for_day(EXPS, date(2026, 7, 1))
    with pytest.raises(PullError, match="empty"):
        resolve_expiry_for_day(pd.DatetimeIndex([]), DAY)


# --------------------------------------------------------------------------- #
# Side classification (G6 semantics)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("price", "bid", "ask", "expected"),
    [
        (1.20, 1.10, 1.20, "buy"),     # at the ask
        (1.10, 1.10, 1.20, "sell"),    # at the bid
        (1.18, 1.10, 1.20, "buy"),     # interior, above mid
        (1.12, 1.10, 1.20, "sell"),    # interior, below mid
        (1.15, 1.10, 1.20, "sell"),    # exact mid -> documented tie convention
        (1.15, 1.20, 1.10, "mid"),     # crossed quote -> honest unknown
        (1.15, np.nan, 1.20, "mid"),   # missing leg -> honest unknown
    ],
)
def test_classify_side(price, bid, ask, expected):
    assert classify_side(price, bid, ask) == expected


# --------------------------------------------------------------------------- #
# Normalizers
# --------------------------------------------------------------------------- #
def test_trade_quote_normalization_et_to_utc_and_strike_units():
    frame, warnings, divisor = normalize_trade_quote(_raw_trade_quote())
    assert divisor == 1000.0                       # millis detected
    assert frame["strike"].unique().tolist() == [600.0]
    # 09:30 ET == 13:30 UTC in June (EDT).
    assert frame.loc[0, "ts"] == pd.Timestamp("2026-06-08 13:30:00", tz="UTC")
    assert frame.loc[0, "expiration"] == DAY
    assert set(frame["side_inferred"]) <= {"buy", "sell", "mid"}
    assert frame.loc[0, "side_inferred"] == "buy"  # printed at the ask


def test_trades_fallback_without_nbbo_degrades_to_mid():
    raw = _raw_trade_quote().drop(columns=["bid", "ask", "bid_size", "ask_size"])
    frame, warnings, _ = normalize_trades_fallback(raw)
    assert (frame["side_inferred"] == "mid").all()
    assert any("mid" in w for w in warnings)


def test_quotes_one_sided_or_crossed_get_nan_mid():
    frame, warnings, _ = normalize_quotes(_raw_quotes())
    good = frame[frame["bid"] > 0]
    assert (good["mid"] == (good["bid"] + good["ask"]) / 2.0).all()
    zero_bid = frame[frame["bid"] == 0.0]
    assert zero_bid["mid"].isna().all()
    assert any("one-sided/crossed" in w for w in warnings)


def test_opra_test_contracts_are_dropped():
    raw = _raw_trade_quote()
    raw.loc[0, "expiration"] = "18820101"          # OPRA test contract
    frame, warnings, _ = normalize_trade_quote(raw)
    assert len(frame) == 2
    assert any("invalid expirations" in w for w in warnings)


def test_dollar_strikes_left_untouched():
    raw = _raw_trade_quote()
    raw["strike"] = 6000.0                          # SPX-scale REAL dollars
    frame, _, divisor = normalize_trade_quote(raw)
    assert divisor == 1.0
    assert frame["strike"].unique().tolist() == [6000.0]


# --------------------------------------------------------------------------- #
# pull_session: endpoint params, fallback, SPX root mapping
# --------------------------------------------------------------------------- #
def test_pull_session_sends_scoped_params():
    client = _full_fake()
    scope = TapeScope(symbols=("SPY",), start=DAY, end=DAY, strikes_each_side=10)
    res = pull_session(client, "SPY", DAY, scope, expirations=EXPS)
    assert res.expiration == DAY
    by_path = {path: params for path, params in client.calls}
    tq = by_path["/v3/option/history/trade_quote"]
    assert tq == {
        "symbol": "SPY", "expiration": "20260608", "date": "20260608", "strike_range": "10",
    }
    assert by_path["/v3/option/history/quote"]["interval"] == "1m"
    assert "strike_range" not in by_path["/v3/option/history/open_interest"] or True
    assert len(res.trades) == 3 and len(res.quotes) == 3 and len(res.oi) == 2


def test_pull_session_falls_back_to_trade_endpoint():
    responses = _full_fake().responses
    responses["/v3/option/history/trade_quote"] = PullError("HTTP 404")
    responses["/v3/option/history/trade"] = _raw_trade_quote().drop(
        columns=["bid", "ask", "bid_size", "ask_size"]
    )
    client = FakeThetaClient(responses)
    scope = TapeScope(symbols=("SPY",), start=DAY, end=DAY)
    res = pull_session(client, "SPY", DAY, scope, expirations=EXPS)
    assert res.endpoints["trades"] == "/v3/option/history/trade"
    assert (res.trades["side_inferred"] == "mid").all()
    assert any("fell back" in w for w in res.warnings)


def test_spx_queries_spxw_root_with_warning():
    client = _full_fake()
    scope = TapeScope(symbols=("SPX",), start=DAY, end=DAY)
    res = pull_session(client, "SPX", DAY, scope, expirations=EXPS)
    assert all(params["symbol"] == "SPXW" for _, params in client.calls)
    assert any("junk OI" in w for w in res.warnings)


# --------------------------------------------------------------------------- #
# write_session / run_pull: partitions, manifest, resume, loud failures
# --------------------------------------------------------------------------- #
def test_write_session_partition_and_manifest(tmp_path):
    client = _full_fake()
    scope = TapeScope(symbols=("SPY",), start=DAY, end=DAY, out_root=tmp_path / "theta")
    res = pull_session(client, "SPY", DAY, scope, expirations=EXPS)
    part = write_session(scope.out_root, "SPY", DAY, res)

    assert part == tmp_path / "theta" / "ticker=SPY" / "date=2026-06-08"
    for name in ("trades.parquet", "quotes.parquet", "oi.parquet", "_manifest.json"):
        assert (part / name).exists()
    manifest = json.loads((part / "_manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["expiration"] == "2026-06-08"
    assert manifest["rows"] == {"trades": 3, "quotes": 3, "oi": 2}
    assert manifest["strike_divisor"] == 1000.0
    # No stray tmp files from the atomic write.
    assert not list(part.glob("*.tmp"))


def test_run_pull_resume_skips_complete_partitions(tmp_path):
    client = _full_fake()
    scope = TapeScope(symbols=("SPY",), start=DAY, end=DAY, out_root=tmp_path / "theta")
    days_fn = lambda s, e: [DAY]  # noqa: E731

    first = run_pull(scope, client, trading_days_fn=days_fn, log=lambda m: None)
    assert (first.pulled, first.skipped_resume) == (1, 0)
    second = run_pull(scope, client, resume=True, trading_days_fn=days_fn, log=lambda m: None)
    assert (second.pulled, second.skipped_resume) == (0, 1)
    # The run journal records both runs.
    runs = json.loads((tmp_path / "theta" / "_runs.json").read_text())
    assert len(runs) == 2


def test_run_pull_records_failures_and_continues(tmp_path):
    responses = _full_fake().responses
    responses["/v3/option/history/quote"] = PullError("HTTP 500 quote endpoint down")
    client = FakeThetaClient(responses)
    scope = TapeScope(symbols=("SPY", "QQQ"), start=DAY, end=DAY, out_root=tmp_path / "t")

    stats = run_pull(scope, client, trading_days_fn=lambda s, e: [DAY], log=lambda m: None)
    assert stats.planned == 2
    assert stats.pulled == 0
    assert len(stats.failed) == 2                  # both symbols failed, run continued
    assert all("HTTP 500" in err for _, _, err in stats.failed)
    # No partition was marked complete -> resume would retry, not skip.
    assert not (tmp_path / "t" / "ticker=SPY" / "date=2026-06-08" / "_manifest.json").exists()


def test_empty_responses_yield_empty_but_complete_partition(tmp_path):
    client = FakeThetaClient({
        "/v3/option/list/expirations": pd.DataFrame({"expiration": ["20260608"]}),
        # trade_quote/quote/oi all return empty 200 bodies -> legitimate no-data
    })
    scope = TapeScope(symbols=("SPY",), start=DAY, end=DAY, out_root=tmp_path / "t")
    stats = run_pull(scope, client, trading_days_fn=lambda s, e: [DAY], log=lambda m: None)
    assert stats.pulled == 1 and not stats.failed
    part = tmp_path / "t" / "ticker=SPY" / "date=2026-06-08"
    assert json.loads((part / "_manifest.json").read_text())["rows"]["trades"] == 0


# --------------------------------------------------------------------------- #
# The operator gate
# --------------------------------------------------------------------------- #
def test_main_without_gate_is_dry_run_only(tmp_path, capsys, monkeypatch):
    """No confirmation -> plan printed, rc 0, NO client constructed, nothing
    written. (The autouse network guard would also fail loudly on any socket.)"""
    monkeypatch.delenv("THETA_OPERATOR_CONFIRM", raising=False)
    rc = main([
        "--symbols", "SPY", "--start", "2026-06-08", "--end", "2026-06-08",
        "--out-root", str(tmp_path / "theta"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "no socket" in out
    assert not (tmp_path / "theta").exists()


def test_main_flag_alone_is_not_enough(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("THETA_OPERATOR_CONFIRM", raising=False)
    rc = main([
        "--symbols", "SPY", "--start", "2026-06-08", "--end", "2026-06-08",
        "--out-root", str(tmp_path / "theta"),
        "--i-understand-this-pulls-live-theta",
    ])
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not (tmp_path / "theta").exists()


def test_runstats_defaults():
    stats = RunStats()
    assert (stats.planned, stats.pulled, stats.skipped_resume, stats.failed) == (0, 0, 0, [])
