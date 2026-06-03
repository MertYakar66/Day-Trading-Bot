"""CLI surface tests: the new version / strategies / doctor commands, the strategy
registry as the single source of truth, and the Windows-safe (ASCII) help text.

All network-free: doctor only inspects the interpreter, the importable SWE
dependency, and local parquet partitions — it never probes Theta or any network.
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.cli import build_parser, main
from intraday.config import EngineConfig
from intraday.data.store import ParquetStore
from intraday.data.synthetic import SyntheticDataProvider
from intraday.signals.registry import (
    STRATEGIES,
    STRATEGY_KEYS,
    UnknownStrategyError,
    build_strategies,
)

TEST_DAY = date(2026, 5, 18)


# --------------------------------------------------------------------------- #
# version / strategies
# --------------------------------------------------------------------------- #
def test_version_subcommand(capsys):
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert "intraday-engine" in out and "python" in out


def test_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    assert "intraday-engine" in capsys.readouterr().out


def test_strategies_subcommand_lists_all(capsys):
    assert main(["strategies"]) == 0
    out = capsys.readouterr().out
    for key in STRATEGY_KEYS:
        assert key in out and STRATEGIES[key].title in out


# --------------------------------------------------------------------------- #
# doctor (env health; never touches the network)
# --------------------------------------------------------------------------- #
def test_doctor_healthy_with_empty_store(tmp_path, capsys):
    rc = main(["doctor", "--store-root", str(tmp_path / "empty")])
    out = capsys.readouterr().out
    assert rc == 0  # python + vendor/swe present in the test env
    assert "Python" in out and "vendor/swe importable" in out
    assert "never connects to Theta" in out
    assert "not found" in out


def test_doctor_reports_ingested_data(tmp_path, capsys):
    root = tmp_path / "store"
    store = ParquetStore(root)
    cfg = EngineConfig.default()
    synth = SyntheticDataProvider(cfg.data, cfg.session)
    store.write_bars(synth.get_bars("SPY", TEST_DAY, "1m"), TEST_DAY)
    rc = main(["doctor", "--store-root", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 symbol(s)" in out and "session partition(s)" in out


def test_doctor_store_root_pointing_at_file(tmp_path, capsys):
    f = tmp_path / "bars_1m.parquet"
    f.write_text("not a store", encoding="utf-8")
    rc = main(["doctor", "--store-root", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "is a file, not a store directory" in out
    assert "not found" not in out  # must not mislabel an existing file


def test_doctor_session_count_ignores_stray_files(tmp_path, capsys):
    root = tmp_path / "store"
    store = ParquetStore(root)
    cfg = EngineConfig.default()
    synth = SyntheticDataProvider(cfg.data, cfg.session)
    store.write_bars(synth.get_bars("SPY", TEST_DAY, "1m"), TEST_DAY)
    # A stray FILE named like a partition must not be counted as a session.
    (root / "bars" / "ticker=SPY" / "date=zzz-stray").write_text("x", encoding="utf-8")
    main(["doctor", "--store-root", str(root)])
    assert "1 symbol(s), 1 session partition(s)" in capsys.readouterr().out


def test_doctor_reports_failure_when_swe_missing(monkeypatch, tmp_path, capsys):
    import sys

    # Force the read-only SWE import to fail (None in sys.modules => ImportError).
    monkeypatch.setitem(sys.modules, "engine.transaction_costs", None)
    rc = main(["doctor", "--store-root", str(tmp_path / "empty")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT importable" in out and "submodule" in out


# --------------------------------------------------------------------------- #
# strategy registry: single source of truth
# --------------------------------------------------------------------------- #
def test_registry_builds_every_strategy():
    built = build_strategies(list(STRATEGY_KEYS), edge=0.1, entry_z=2.0, stop_k=1.0)
    assert len(built) == len(STRATEGY_KEYS)
    # Each built object satisfies the Strategy protocol (has a propose + id).
    assert all(hasattr(s, "propose") and hasattr(s, "strategy_id") for s in built)


def test_registry_unknown_strategy_raises():
    with pytest.raises(UnknownStrategyError, match="unknown strategy"):
        build_strategies(["s9"], edge=0.1, entry_z=2.0, stop_k=1.0)


def test_cli_strategy_choices_match_registry():
    """argparse --strategy choices must be exactly the registry keys (no drift)."""
    parser = build_parser()
    ns = parser.parse_args(["backtest", "--strategy", *STRATEGY_KEYS,
                            "--start", "2026-05-04", "--end", "2026-05-06"])
    assert ns.strategy == list(STRATEGY_KEYS)


def test_cli_rejects_unknown_strategy_choice():
    with pytest.raises(SystemExit):  # argparse 'choices' rejects before we build
        build_parser().parse_args(["backtest", "--strategy", "s9"])


# --------------------------------------------------------------------------- #
# Windows-safe help: no non-ASCII anywhere argparse might print
# --------------------------------------------------------------------------- #
def test_main_help_is_ascii():
    # A non-ASCII char here (e.g. an em-dash / arrow) crashes --help on a cp1252
    # Windows console — assert the help is pure ASCII so it never can.
    build_parser().format_help().encode("ascii")


@pytest.mark.parametrize("cmd", ["backtest", "report", "compare", "report-index",
                                 "version", "strategies", "doctor"])
def test_subcommand_help_is_ascii(cmd, capsys):
    with pytest.raises(SystemExit):
        main([cmd, "--help"])
    capsys.readouterr().out.encode("ascii")  # raises if any non-ASCII slipped in


def test_backtest_runtime_output_is_ascii(capsys):
    """The RUNTIME report (metrics banner + honesty scorecard) must be ASCII too:
    an em-dash in the 'SYNTHETIC DATA - NOT A REAL EDGE' banner renders as mojibake
    on a cp1252 Windows console. The help-text ASCII tests above did not cover this
    output path."""
    rc = main(["backtest", "--start", "2026-05-04", "--end", "2026-05-06"])
    out = capsys.readouterr().out
    assert rc == 0
    out.encode("ascii")  # raises if any non-ASCII reached the console output
    assert "SYNTHETIC DATA" in out  # the honesty banner is present (and now ASCII-safe)
    assert "VERDICT" in out         # the scorecard renders on a real (non-empty) run


# --------------------------------------------------------------------------- #
# No-data guard: a zero-trading-day run must not masquerade as 'no edge'
# --------------------------------------------------------------------------- #
def test_backtest_no_data_warns_and_suppresses_verdict(capsys):
    # Reversed dates => trading_days() == [] => a zero-day run.
    rc = main(["backtest", "--start", "2026-05-29", "--end", "2026-05-01"])
    cap = capsys.readouterr()
    assert rc == 2                                   # non-zero: the operator made a mistake
    assert "no trading days" in cap.err.lower()
    assert "no-data run" in cap.err.lower()
    assert "VERDICT" not in cap.out                  # the meaningless verdict is suppressed


def test_report_no_data_does_not_write_html(tmp_path, capsys):
    out = tmp_path / "dash.html"
    rc = main(["report", "--start", "2026-05-29", "--end", "2026-05-01",
               "--out", str(out)])
    assert rc == 2
    assert "no trading days" in capsys.readouterr().err.lower()
    assert not out.exists()  # never write a dashboard whose verdict would misread as 'no edge'


# --------------------------------------------------------------------------- #
# Duplicate --strategy keys must not inflate the trial count
# --------------------------------------------------------------------------- #
def test_dedup_strategies_preserves_first_seen_order():
    from intraday.cli import _dedup_strategies

    assert _dedup_strategies(["s4", "s3", "s4", "s3", "s5"]) == ["s4", "s3", "s5"]


def test_backtest_duplicate_strategy_runs_one_trial(capsys):
    rc = main(["backtest", "--strategy", "s3", "s3",
               "--start", "2026-05-04", "--end", "2026-05-06"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "n_trials=1" in out  # deduped to a single distinct trial, not 2


# --------------------------------------------------------------------------- #
# doctor verifies the core scientific stack (so a store read can't crash post-OK)
# --------------------------------------------------------------------------- #
def test_doctor_checks_scientific_stack(tmp_path, capsys):
    main(["doctor", "--store-root", str(tmp_path / "empty")])
    out = capsys.readouterr().out
    assert "numpy importable" in out
    assert "pandas importable" in out
    assert "scipy importable" in out
