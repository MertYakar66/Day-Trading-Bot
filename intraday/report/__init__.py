"""Self-contained HTML dashboard for intraday backtests (paper-only).

``build_dashboard(result)`` turns a finished backtest into a single offline HTML
file — KPI cards, equity/drawdown/daily-PnL charts (inline SVG, no dependencies),
the full honesty scorecard (clustered-t, bootstrap-CI Sharpe, Deflated Sharpe),
cost attribution, a trade blotter, and an optional cross-sectional universe view.

Run it from the CLI::

    python -m intraday report --start 2026-05-01 --end 2026-05-29 --out dashboard.html

See :mod:`intraday.report.svg` for the chart primitives and
:mod:`intraday.report.dashboard` for the document assembler.
"""

from __future__ import annotations

from .dashboard import build_dashboard, render_dashboard

__all__ = ["build_dashboard", "render_dashboard"]
