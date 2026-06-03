"""Regenerate the illustrative (SYNTHETIC) sample reports committed under docs/.

These are pre-rendered so a reader can open them without running anything. They use
the deterministic synthetic provider and a FIXED ``generated_at`` so re-running this
produces byte-identical files (no churn in git unless the report itself changes).

    python -m scripts.gen_samples

PAPER ONLY, synthetic data, no network. Never run against real/Theta data — the
samples are explicitly labelled SYNTHETIC and must stay that way.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.data.synthetic import SyntheticDataProvider
from intraday.report import build_comparison, build_dashboard, build_index
from intraday.signals.registry import build_strategy

DOCS = Path(__file__).resolve().parent.parent / "docs"
GEN = "2026-06-02 (illustrative; synthetic data)"
# A window long enough to populate the rolling-Sharpe view; multi-symbol so the
# per-symbol breakdown renders too.
START, END = date(2026, 4, 1), date(2026, 5, 29)
SYMBOLS = ["SPY", "QQQ"]


def _run(strategy_keys: list[str]):
    cfg = EngineConfig.default()
    provider = SyntheticDataProvider(cfg.data, cfg.session)
    strategies = [build_strategy(k, edge=0.5, entry_z=0.1, stop_k=1.0) for k in strategy_keys]
    return IntradayBacktester(cfg, provider, strategies).run(SYMBOLS, START, END, "5m")


def main() -> int:
    run_meta = {"start": START.isoformat(), "end": END.isoformat()}

    dash = build_dashboard(
        _run(["s3"]), n_trials=1, generated_at=GEN,
        run_meta={**run_meta, "strategies": "s3"},
        title="Intraday engine - sample dashboard (SYNTHETIC)",
    )
    # newline="\n": write LF on every OS so re-running is byte-identical cross-platform
    # (matches the repo's .gitattributes eol=lf; no CRLF churn).
    (DOCS / "sample_dashboard.html").write_text(dash, encoding="utf-8", newline="\n")

    runs = [(k, _run([k])) for k in ("s3", "s4", "s5")]
    cmp_html = build_comparison(
        runs, generated_at=GEN, run_meta={**run_meta, "strategies": "s3 s4 s5"},
        title="Intraday engine - sample comparison (SYNTHETIC)",
    )
    (DOCS / "sample_comparison.html").write_text(cmp_html, encoding="utf-8", newline="\n")

    idx = build_index(
        DOCS, generated_at=GEN, index_name="sample_index.html",
        title="Intraday engine - sample report index",
    )
    (DOCS / "sample_index.html").write_text(idx, encoding="utf-8", newline="\n")

    for name in ("sample_dashboard.html", "sample_comparison.html", "sample_index.html"):
        p = DOCS / name
        print(f"wrote {p} ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
