"""Self-contained HTML reports for intraday backtests (paper-only).

- ``build_dashboard(result)`` — a single offline dashboard for one backtest
  (KPIs, equity/drawdown/daily-PnL charts, honesty scorecard, cost waterfall,
  trade blotter, optional cross-sectional universe view).
- ``build_comparison(runs)`` — overlay several strategies on one page (normalised
  equity curves + a ranked metrics table).
- ``build_index(dir)`` — a static index linking every report in a directory.
- ``build_summary(result)`` — a machine-readable JSON summary (the dashboard's
  data sibling).

Everything is dependency-free (charts are pure-Python inline SVG in
:mod:`intraday.report.svg`), deterministic, HTML-escaped, and fully offline.

From the CLI::

    python -m intraday report  --out dashboard.html
    python -m intraday compare --strategy s3 s4 s5 --out compare.html
    python -m intraday report-index --dir reports/ --out reports/index.html
"""

from __future__ import annotations

from .comparison import build_comparison, render_comparison
from .dashboard import build_dashboard, render_dashboard
from .export import build_summary, summary_dict
from .index_page import build_index, render_index, scan_directory

__all__ = [
    "build_dashboard",
    "render_dashboard",
    "build_comparison",
    "render_comparison",
    "build_summary",
    "summary_dict",
    "build_index",
    "render_index",
    "scan_directory",
]
