"""Tests for the scoped Theta options PULL PLANNER (scripts.pull_theta_options_scoped).

The planner is pure work-list logic with a hard safety gate; it must NEVER pull
live Theta. We verify the scoping (symbols × sessions, ATM±N, expiries), the
estimate, and that 'execute' is refused unless explicitly confirmed (and even then
stops rather than fabricating the SWE CLI / opening a socket).
"""

from __future__ import annotations

from datetime import date

import pytest

from scripts.pull_theta_options_scoped import (
    PullScope,
    build_pull_plan,
    estimate,
    main,
)


def test_plan_is_symbols_times_sessions():
    scope = PullScope(symbols=("SPX", "SPY"), start=date(2026, 5, 4), end=date(2026, 5, 8))
    plan = build_pull_plan(scope)
    # Mon..Fri 2026-05-04..08 = 5 trading days × 2 symbols.
    assert len(plan) == 10
    assert {it.symbol for it in plan} == {"SPX", "SPY"}
    assert {it.day for it in plan} == {
        date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8),
    }
    assert all(it.strikes_each_side == 10 for it in plan)
    assert all(it.expiries == "0dte+near" for it in plan)


def test_command_template_targets_swe_puller():
    plan = build_pull_plan(PullScope(symbols=("SPY",), start=date(2026, 5, 4), end=date(2026, 5, 4)))
    cmd = plan[0].swe_command_template()
    assert "vendor/swe/scripts/pull_theta_option_tape.py" in cmd
    assert "--symbol SPY" in cmd
    assert "2026-05-04" in cmd


def test_estimate_scales_with_plan():
    small = build_pull_plan(PullScope(symbols=("SPY",), start=date(2026, 5, 4), end=date(2026, 5, 4)))
    big = build_pull_plan(PullScope(symbols=("SPX", "SPY", "QQQ"), start=date(2026, 5, 4), end=date(2026, 5, 8)))
    assert estimate(big)["pulls"] > estimate(small)["pulls"]
    assert estimate(small)["pulls"] == 1


def test_main_dry_run_returns_zero(capsys):
    rc = main(["--symbols", "SPY", "--start", "2026-05-04", "--end", "2026-05-05"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "no socket" in out.lower()


def test_main_refuses_execute_without_env(capsys, monkeypatch):
    monkeypatch.delenv("THETA_OPERATOR_CONFIRM", raising=False)
    # Confirm flag set but env missing → still a dry run (no execution).
    rc = main(["--symbols", "SPY", "--start", "2026-05-04", "--end", "2026-05-04",
               "--i-understand-this-pulls-live-theta"])
    assert rc == 0


def test_main_confirmed_stops_without_fabricating(monkeypatch):
    monkeypatch.setenv("THETA_OPERATOR_CONFIRM", "1")
    with pytest.raises(SystemExit) as exc:
        main(["--symbols", "SPY", "--start", "2026-05-04", "--end", "2026-05-04",
              "--i-understand-this-pulls-live-theta"])
    # It stops with operator guidance rather than opening a socket.
    assert "not wired" in str(exc.value).lower()
