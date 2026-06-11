"""Side-by-side multi-strategy comparison page (e.g. S3 vs S4 vs S5).

Runs are compared on one set of axes: normalised equity curves overlaid (each as
cumulative % return from its starting capital, so they are directly comparable),
plus a ranked table of the headline + honesty metrics. Comparing *N* strategies is
itself *N* trials, so each strategy's Deflated Sharpe is computed with
``n_trials = N`` — the comparison cannot launder a best-of-N pick into an "edge".

Reuses the dashboard's formatting/banner helpers and the shared theme, so the
look-and-feel and the real-vs-synthetic labelling are identical across pages.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..backtest.engine import BacktestResult
from ..eval import evaluate_result
from ..eval.stats import INSUFFICIENT_DATA_MIN_DAYS, edge_verdict
from ..metrics import build_report
from . import svg
from .dashboard import _banner, _cls, _esc, _fnum, _signed_money
from .theme import document


def _norm_returns(result: BacktestResult) -> list[float]:
    """Equity curve as cumulative fractional return from starting capital."""
    cap = result.initial_capital or 1.0
    return [float(r["portfolio_value"]) / cap - 1.0 for r in result.equity_curve]


def _legend(names: Sequence[str]) -> str:
    items = []
    for i, name in enumerate(names):
        color = svg.SERIES_PALETTE[i % len(svg.SERIES_PALETTE)]
        items.append(
            f'<span class="legend__item"><span class="legend__swatch" '
            f'style="background:{color}"></span>{_esc(name)}</span>'
        )
    return '<div class="legend">' + "".join(items) + "</div>"


def _verdict_badge(ev) -> str:
    # Route each row through the centralized three-state verdict so a too-short
    # run reads "insufficient data" here exactly as it does on every other surface
    # (a binary badge would print a definitive "no edge" the page itself disclaims).
    verdict = edge_verdict(ev)
    if verdict == "EDGE":
        return '<span class="badge badge--edge">EDGE</span>'
    if verdict == "INSUFFICIENT_DATA":
        return '<span class="badge badge--nodata">insufficient data</span>'
    return '<span class="badge badge--noedge">no edge</span>'


def _comparison_table(entries: Sequence[Mapping], best_idx: int) -> str:
    head = (
        "<tr><th>strategy</th><th>net $</th><th>gross $</th><th>Sharpe</th>"
        "<th>t</th><th>P(SR&gt;0)</th><th>DSR</th><th>max DD</th><th>win%</th>"
        "<th>trades</th><th>verdict</th></tr>"
    )
    rows = []
    for i, e in enumerate(entries):
        m, ev = e["metrics"], e["ev"]
        color = svg.SERIES_PALETTE[i % len(svg.SERIES_PALETTE)]
        cls = ' class="cmp-best"' if i == best_idx else ""
        rows.append(
            f"<tr{cls}>"
            f'<td><span class="legend__swatch" style="background:{color}"></span> '
            f'{_esc(e["name"])}</td>'
            f'<td class="{_cls(m.net_pnl)}">{_signed_money(m.net_pnl)}</td>'
            f'<td class="{_cls(m.gross_pnl)}">{_signed_money(m.gross_pnl)}</td>'
            f'<td class="{_cls(m.sharpe_ratio)}">{_fnum(m.sharpe_ratio)}</td>'
            f"<td>{_fnum(ev.t_stat)}</td>"
            f'<td>{_fnum(ev.psr_vs_zero, "{:.3f}")}</td>'
            f'<td>{_fnum(ev.deflated_sharpe, "{:.3f}")}</td>'
            f'<td class="neg">{m.max_drawdown:.2%}</td>'
            f"<td>{m.win_rate:.1%}</td>"
            f"<td>{m.total_trades}</td>"
            f"<td>{_verdict_badge(ev)}</td>"
            "</tr>"
        )
    return (
        '<table class="cmp"><thead>' + head + "</thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )


def render_comparison(
    entries: Sequence[Mapping],
    *,
    title: str = "Intraday engine — strategy comparison",
    generated_at: str | None = None,
    run_meta: Mapping | None = None,
) -> str:
    """Render the comparison page from pre-built ``entries`` (name/result/metrics/ev)."""
    if not entries:
        return document(title, "<h1>" + _esc(title) + "</h1><p class='muted'>No runs.</p>")

    names = [e["name"] for e in entries]
    series = [_norm_returns(e["result"]) for e in entries]
    # x labels from the run with the most points (they share a trading-day grid)
    longest = max(entries, key=lambda e: len(e["result"].equity_curve))["result"]
    dates = [str(r["date"])[:10] for r in longest.equity_curve]

    # "best" = highest annualised Sharpe (risk-adjusted), tie-broken by net PnL.
    best_idx = max(
        range(len(entries)),
        key=lambda i: (entries[i]["ev"].sharpe_ann, entries[i]["metrics"].net_pnl),
    )

    any_edge = any(e["ev"].significant for e in entries)
    insufficient = all(e["ev"].n_days < INSUFFICIENT_DATA_MIN_DAYS for e in entries)
    rm = run_meta or {}
    first = entries[0]["result"]
    meta_bits = [
        f"symbols <b>{_esc(', '.join(first.symbols))}</b>",
        f"interval <b>{_esc(first.interval)}</b>",
        f"strategies <b>{_esc(', '.join(names))}</b>",
        f"window <b>{_esc(rm.get('start', dates[0] if dates else '?'))} "
        f"→ {_esc(rm.get('end', dates[-1] if dates else '?'))}</b>",
    ]
    gen = _esc(generated_at) if generated_at else "—"
    n_trials = entries[0]["ev"].n_trials

    chart = svg.multi_line_chart(
        series, baseline=0.0, labels=dates, value_fmt=lambda v: f"{v:.1%}",
        title="Strategy cumulative % return (net of costs)",
    )

    if insufficient:
        verdict = (
            '<div class="verdict verdict--nodata"><span class="verdict__tag">'
            "INSUFFICIENT DATA</span><span class=\"verdict__detail\">No strategy has "
            "enough trading days to evaluate &mdash; a NO-DATA condition, not a "
            "'no edge' result.</span></div>"
        )
    elif any_edge:
        verdict = (
            '<div class="verdict verdict--edge"><span class="verdict__tag">'
            "AT LEAST ONE EDGE</span><span class=\"verdict__detail\">A strategy "
            f"clears Deflated Sharpe &ge; 0.95 across the {n_trials}-strategy "
            "comparison.</span></div>"
        )
    else:
        verdict = (
            '<div class="verdict verdict--noedge"><span class="verdict__tag">NO '
            "EDGE</span><span class=\"verdict__detail\">No strategy clears Deflated "
            f"Sharpe &ge; 0.95 across the {n_trials}-strategy comparison (the "
            "best Sharpe row is highlighted, but a best-of-N pick is not an edge).</span></div>"
        )

    body = f"""<h1>{_esc(title)}</h1>
<p class="sub">{' &middot; '.join(meta_bits)} &middot; generated {gen}</p>
{_banner(first)}
{verdict}

<section class="card">
<h2>Equity curves (cumulative % return, net of costs)</h2>
{_legend(names)}
{chart}
</section>

<section class="card">
<h2>Ranked metrics (best Sharpe highlighted)</h2>
<div class="scroll-x">{_comparison_table(entries, best_idx)}</div>
</section>

<p class="foot">
Paper-only. Curves are cumulative % return from each strategy's starting capital,
net of modelled costs. Comparing {n_trials} strategies is {n_trials} trials, so each
Deflated Sharpe already carries that multiple-testing penalty (Bailey &amp;
L&oacute;pez de Prado). Highlighting = highest annualised Sharpe, which is NOT the
same as a demonstrated edge. The t-stat / PSR / DSR assume serially-independent daily
PnL (autocorrelation biases them upward); the bootstrap Sharpe CI is robust to
short-range serial dependence (the counterweight). No broker orders; renders fully offline.
</p>"""
    return document(title, body)


def build_comparison(
    runs: Mapping[str, BacktestResult] | Sequence[tuple[str, BacktestResult]],
    *,
    n_trials: int | None = None,
    title: str = "Intraday engine — strategy comparison",
    generated_at: str | None = None,
    run_meta: Mapping | None = None,
) -> str:
    """Compute metrics + honesty eval for each run, then render the comparison page.

    ``n_trials`` defaults to the number of strategies compared (the honest
    multiple-testing count for a side-by-side selection)."""
    items = list(runs.items()) if isinstance(runs, Mapping) else list(runs)
    n = n_trials if n_trials is not None else max(len(items), 1)
    entries = [
        {
            "name": name,
            "result": res,
            "metrics": build_report(res),
            "ev": evaluate_result(res, n_trials=n),
        }
        for name, res in items
    ]
    return render_comparison(entries, title=title, generated_at=generated_at, run_meta=run_meta)
