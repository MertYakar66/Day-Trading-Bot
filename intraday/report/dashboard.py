"""Self-contained HTML dashboard for an intraday backtest (paper-only).

Turns a finished :class:`~intraday.backtest.engine.BacktestResult` and its honesty
:class:`~intraday.eval.stats.StrategyEval` into a **single offline HTML file**:
KPI cards, an equity curve, an underwater drawdown, signed daily-PnL bars, a
cost-attribution waterfall, the full honesty scorecard (clustered-t, bootstrap-CI
Sharpe, and the multiple-testing-aware Deflated Sharpe), a trade blotter, and an
optional cross-sectional universe section.

Design rules that make this trustworthy and testable:

- **No external resources.** All CSS is inlined; all charts are inline SVG
  (:mod:`intraday.report.svg`). The file opens with no network — nothing is
  fetched from a CDN, so it cannot silently change or phone home.
- **Real vs SYNTHETIC is shouted, never whispered.** A synthetic run gets a loud
  banner so a fixture can never be mistaken for a real edge — mirroring
  :meth:`intraday.metrics.MetricsReport.render`.
- **The verdict is the eval's, not the author's.** The headline EDGE / NO-EDGE
  band is driven solely by ``StrategyEval.significant`` (deflated Sharpe >= 0.95).
- **Deterministic.** Given the same data and ``generated_at`` string, the output
  is byte-identical (no clock, no randomness inside).
- **Escaped.** Every interpolated string (symbols, exit reasons, labels) is HTML
  escaped — a backtest cannot inject markup into its own report.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from html import escape

from ..backtest.engine import BacktestResult
from ..eval import daily_pnl_from_result, evaluate_result
from ..eval.stats import INSUFFICIENT_DATA_MIN_DAYS, StrategyEval
from ..metrics import MetricsReport, build_report
from . import svg
from .theme import document


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #
def _money(v: float) -> str:
    return f"${v:,.0f}" if abs(v) >= 1000 else f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v:.2%}"


def _signed_money(v: float) -> str:
    return ("+" if v >= 0 else "−") + _money(abs(v))


def _chart_money(v: float) -> str:
    """Currency for CHART labels: the minus sign goes BEFORE the '$' ('−$6,169'),
    matching the KPI/:func:`_signed_money` convention. Without this, a chart's
    default ``f"${v:,.0f}"`` renders '$-6,169' while the KPIs render '−$6,169' for
    the very same number, on the same page. No '+' on positives (axis ticks would be
    noisy); deterministic (no locale). Non-finite renders as an em-dash (insurance —
    every current call site already drops/zeroes non-finite). The minus is gated at
    ``v <= -0.5`` so a sub-dollar magnitude that rounds to 0 is never shown as '−$0'."""
    if not math.isfinite(v):
        return "—"
    return ("−$" if v <= -0.5 else "$") + f"{abs(v):,.0f}"


# The documented MAXIMUM multiple-testing budget the honest full-universe evaluation
# searches (3 underlying-only strategies × the 24-symbol cross-section = up to 72
# trials; see scripts/eval_real_universe.py — the actual count is data-dependent, as
# only strategy×symbol combos with >= 2 days become trials). A single-strategy report
# can run with n_trials as low as 1, where a green EDGE is conditional on a far
# smaller search than the headline run — the verdict band says so. Reference value;
# update if the documented universe changes.
_FULL_UNIVERSE_TRIALS = 72


def _cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _fnum(v, fmt: str = "{:.2f}", dash: str = "—") -> str:
    """Format a number, but render a non-finite/missing value as an em-dash instead
    of the bare string ``"nan"`` (which a missing universe field would otherwise show)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return dash
    if x != x or x in (float("inf"), float("-inf")):
        return dash
    return fmt.format(x)


def _esc(s) -> str:
    return escape(str(s))


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def _drawdown_curve(equity: Sequence[float]) -> list[float]:
    """Underwater curve: fractional drawdown from the running peak (<= 0)."""
    out: list[float] = []
    peak = float("-inf")
    for v in equity:
        peak = max(peak, v)
        out.append((v / peak - 1.0) if peak > 0 else 0.0)
    return out


def _banner(result: BacktestResult) -> str:
    src = result.data_source
    if src.value == "synthetic":
        return (
            '<div class="banner banner--synthetic">SYNTHETIC DATA &mdash; NOT A REAL '
            "EDGE. Deterministic fixture for plumbing/causality tests only.</div>"
        )
    return (
        f'<div class="banner banner--real">REAL DATA &mdash; source: '
        f"<b>{_esc(src.value)}</b>. Net of modelled costs; paper-only (no broker "
        "orders were placed).</div>"
    )


def _verdict_band(ev: StrategyEval) -> str:
    # No / too-little data is NOT a 'no edge' finding — render it distinctly so a
    # zero- or one-day run can never read as the product's scientific verdict.
    if ev.n_days < INSUFFICIENT_DATA_MIN_DAYS:
        return (
            '<div class="verdict verdict--nodata"><span class="verdict__tag">'
            "INSUFFICIENT DATA</span><span class=\"verdict__detail\">Only "
            f"{ev.n_days} trading day(s) evaluated &mdash; too few to judge an edge. "
            "This is a NO-DATA condition, not a 'no demonstrated edge' result.</span></div>"
        )
    dsr = _fnum(ev.deflated_sharpe, "{:.3f}")
    if ev.significant:
        # A green EDGE on few trials is the most plausible way to accidentally
        # manufacture a positive verdict — disclose the search budget it is conditional on.
        caveat = (
            f" Conditional on only {ev.n_trials} trial(s): the honest full-universe "
            f"search spans up to {_FULL_UNIVERSE_TRIALS} strategy&times;symbol trials "
            "(data-dependent; scripts/eval_real_universe.py), which would deflate this "
            "further."
            if ev.n_trials < _FULL_UNIVERSE_TRIALS else ""
        )
        return (
            '<div class="verdict verdict--edge"><span class="verdict__tag">EDGE '
            "(rare)</span><span class=\"verdict__detail\">Deflated Sharpe "
            f"{dsr} &ge; 0.95 across {ev.n_trials} trial(s) &mdash; "
            f"survives the multiple-testing penalty.{caveat}</span></div>"
        )
    return (
        '<div class="verdict verdict--noedge"><span class="verdict__tag">NO '
        "DEMONSTRATED EDGE</span><span class=\"verdict__detail\">Deflated Sharpe "
        f"{dsr} &lt; 0.95 across {ev.n_trials} trial(s). The "
        "net result is not statistically distinguishable from luck after costs.</span></div>"
    )


def _kpi(label: str, value: str, sub: str = "", *, cls: str = "") -> str:
    # value/sub are escaped too (the module's invariant: nothing interpolated is raw HTML).
    sub_html = f'<div class="kpi__sub">{_esc(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi"><div class="kpi__label">{_esc(label)}</div>'
        f'<div class="kpi__value {cls}">{_esc(value)}</div>{sub_html}</div>'
    )


def _signed_cls(v: float) -> str:
    """Colour class for a directional value, plus the ``signed`` opt-in that adds a
    non-chromatic up/down chevron (colour-blind aid) — only on values where a
    positive/negative direction is meaningful (not costs / drawdown magnitudes).

    A non-finite value (e.g. an infinite Calmar/Sortino, rendered as an em-dash by
    :func:`_fnum`) gets no class at all, so no misleading chevron/colour is implied.
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(x):
        return ""
    return f"{_cls(x)} signed"


def _kpi_grid(m: MetricsReport, ev: StrategyEval) -> str:
    cards = [
        _kpi("Net PnL", _signed_money(m.net_pnl), "after modelled costs",
             cls=_signed_cls(m.net_pnl)),
        _kpi("Gross PnL", _signed_money(m.gross_pnl), "before costs",
             cls=_signed_cls(m.gross_pnl)),
        _kpi("Total costs", _money(m.total_costs),
             f"{m.cost_bps_of_notional:.1f} bps of notional", cls="neg"),
        _kpi("Sharpe (net, ann.)", f"{m.sharpe_ratio:.2f}",
             f"95% CI [{ev.sharpe_ann_ci_lo:.2f}, {ev.sharpe_ann_ci_hi:.2f}]",
             cls=_signed_cls(m.sharpe_ratio)),
        _kpi("Sortino (net, ann.)", _fnum(m.sortino_ratio), "downside volatility only",
             cls=_signed_cls(m.sortino_ratio)),
        _kpi("Max drawdown", _pct(m.max_drawdown), "peak-to-trough (net equity)",
             cls="neg"),
        _kpi("Calmar", _fnum(m.calmar_ratio), "ann. return / max drawdown",
             cls=_signed_cls(m.calmar_ratio)),
        _kpi("Win rate", f"{m.win_rate:.1%}", f"{m.total_trades} trades"),
        _kpi("Expectancy / trade", _signed_money(m.expectancy_per_trade), "net $/trade",
             cls=_signed_cls(m.expectancy_per_trade)),
        _kpi("Trading days", f"{m.n_days}",
             f"NAV ${m.initial_capital:,.0f} → ${m.final_equity:,.0f}"),
    ]
    return '<div class="kpis">' + "".join(cards) + "</div>"


def _scorecard(ev: StrategyEval) -> str:
    rows = [
        ("Trading-day t-stat", f"{ev.t_stat:.2f}",
         f"p = {ev.p_value:.3f} over n = {ev.n_days} days (clustered: one observation per day)"),
        ("Sharpe (annualised, net)", f"{ev.sharpe_ann:.2f}",
         f"95% bootstrap CI [{ev.sharpe_ann_ci_lo:.2f}, {ev.sharpe_ann_ci_hi:.2f}] "
         "(stationary block bootstrap)"),
        ("Daily-PnL skew / kurtosis", f"{ev.skew:.2f} / {ev.kurtosis:.2f}",
         "shape of the return distribution (kurtosis 3 = normal)"),
        ("P(true Sharpe &gt; 0)", f"{ev.psr_vs_zero:.3f}",
         "Probabilistic Sharpe Ratio vs zero (single trial)"),
        ("Deflated Sharpe", _fnum(ev.deflated_sharpe, "{:.3f}"),
         f"P(true Sharpe &gt; 0) after the {ev.n_trials}-trial selection penalty "
         "(Bailey &amp; López de Prado) &mdash; edge iff &ge; 0.95"),
    ]
    body = "".join(
        f'<tr><td class="sc__name">{name}</td>'
        f'<td class="sc__val {("pos" if name.startswith("Deflated") and ev.significant else "")}">{val}</td>'
        f'<td class="sc__note">{note}</td></tr>'
        for name, val, note in rows
    )
    if ev.n_days < INSUFFICIENT_DATA_MIN_DAYS:
        verdict, vcls = "insufficient data", ""
    else:
        verdict = "EDGE" if ev.significant else "NO demonstrated edge"
        vcls = "pos" if ev.significant else "neg"
    # Honest disclosure: the parametric statistics assume serially-independent days;
    # the bootstrap CI is the autocorrelation-robust counterweight (it does not drive
    # the verdict). See intraday.eval.stats.
    iid_note = (
        '<p class="muted">The t-stat, P(true Sharpe&gt;0) and Deflated Sharpe assume '
        "serially-independent daily PnL; positive day-to-day autocorrelation biases "
        "them upward (overstating significance). The 95% Sharpe CI uses a stationary "
        "block bootstrap, which is robust to short-range serial dependence &mdash; "
        "read it as the counterweight.</p>"
    )
    return (
        '<table class="scorecard"><thead><tr><th>Metric</th><th>Value</th>'
        "<th>What it means</th></tr></thead><tbody>"
        + body
        + f'<tr class="sc__verdict"><td class="sc__name">VERDICT</td>'
        f'<td class="sc__val {vcls}">{verdict}</td>'
        '<td class="sc__note">the deflated Sharpe is the sole authority here</td></tr>'
        "</tbody></table>"
        + iid_note
    )


def _cost_attribution(m: MetricsReport) -> str:
    chart = svg.waterfall(
        [
            ("gross", m.gross_pnl, "total"),
            ("costs", -m.total_costs, "delta"),
            ("net", m.net_pnl, "total"),
        ],
        value_fmt=_chart_money,
        title="Cost attribution: gross PnL minus costs equals net PnL",
    )
    facts = (
        '<ul class="facts">'
        f"<li>cost / NAV<span>{m.cost_pct_of_nav:.2%}</span></li>"
        f"<li>cost / trade<span>{_money(m.cost_per_trade)}</span></li>"
        f"<li>cost (bps of notional)<span>{m.cost_bps_of_notional:.1f} bps</span></li>"
        f"<li>turnover<span>{m.turnover:.2f}× NAV/day</span></li>"
        f"<li>profit factor<span>{m.profit_factor:.2f}</span></li>"
        f"<li>payoff ratio<span>{m.payoff_ratio:.2f}</span></li>"
        "</ul>"
    )
    return f'<div class="split"><div class="split__chart">{chart}</div>{facts}</div>'


def _trade_blotter(result: BacktestResult, *, limit: int = 200) -> str:
    trades = sorted(result.trades, key=lambda t: (str(t.entry_ts), t.symbol))
    total = len(trades)
    if total == 0:
        return ('<div class="empty-state">No trades were taken &mdash; the net-of-cost '
                "expectancy gate refused every signal. With no proven edge that is the "
                "correct, honest outcome.</div>")
    shown = trades[:limit]

    def _ts(ts) -> str:
        s = str(ts)
        return _esc(s[:16].replace("T", " "))

    rows = []
    for t in shown:
        rows.append(
            "<tr>"
            f"<td>{_esc(t.symbol)}</td><td>{_esc(t.strategy_id)}</td>"
            f'<td class="{_cls(t.side.sign)}">{_esc(t.side.value)}</td>'
            f"<td class=\"num\">{t.size}</td>"
            f'<td class="num">{_ts(t.entry_ts)}</td><td class="num">{_ts(t.exit_ts)}</td>'
            f'<td class="num">{t.entry_price:,.2f}</td><td class="num">{t.exit_price:,.2f}</td>'
            f'<td class="num {_cls(t.gross_pnl)}">{_signed_money(t.gross_pnl)}</td>'
            f'<td class="num neg">{_money(t.costs)}</td>'
            f'<td class="num {_cls(t.net_pnl)}">{_signed_money(t.net_pnl)}</td>'
            f"<td>{_esc(t.exit_reason)}</td></tr>"
        )
    note = (
        f'<p class="muted">Showing first {limit} of {total} trades '
        "(sorted by entry time).</p>"
        if total > limit
        else ""
    )
    return (
        '<table class="blotter"><thead><tr><th>symbol</th><th>strat</th><th>side</th>'
        "<th>size</th><th>entry</th><th>exit</th><th>entry px</th><th>exit px</th>"
        "<th>gross</th><th>costs</th><th>net</th><th>exit reason</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + note
    )


def _exit_reasons(m: MetricsReport) -> str:
    if not m.exit_reason_counts:
        return ""
    items = sorted(m.exit_reason_counts.items(), key=lambda kv: -kv[1])
    total = sum(c for _, c in items) or 1
    bars = []
    for reason, count in items:
        pct = count / total
        bars.append(
            f'<li><span class="er__label">{_esc(reason)}</span>'
            f'<span class="er__bar"><span style="width:{pct:.1%}"></span></span>'
            f'<span class="er__count">{count}</span></li>'
        )
    return '<ul class="exitreasons">' + "".join(bars) + "</ul>"


_ROLL_WINDOW = 10


def _rolling_sharpe(daily_vals: Sequence[float], window: int = _ROLL_WINDOW) -> list[float]:
    """Trailing-``window`` annualised Sharpe at each day (one value per window)."""
    from ..eval import annualized_sharpe

    return [
        annualized_sharpe(daily_vals[i - window + 1 : i + 1])
        for i in range(window - 1, len(daily_vals))
    ]


def _rolling_sharpe_section(
    daily_vals: Sequence[float], daily_dates: Sequence[str], window: int = _ROLL_WINDOW
) -> str:
    """Show how the (annualised) Sharpe holds up over the window — a decaying or
    wildly swinging line is a red flag a single headline Sharpe would hide. Rendered
    only when there are enough days for the rolling view to mean anything."""
    if len(daily_vals) < window + 2:
        return ""
    roll = _rolling_sharpe(daily_vals, window)
    roll_dates = list(daily_dates)[window - 1:]
    chart = svg.line_chart(roll, baseline=0.0, labels=roll_dates,
                           value_fmt=lambda v: f"{v:.2f}",
                           title=f"Rolling {window}-day annualised Sharpe")
    return (
        '<section class="card">'
        f"<h2>Rolling Sharpe ({window}-day, annualised net)</h2>"
        f"{chart}"
        '<p class="muted">Flat near zero is the honest expectation without an edge; a '
        "persistently positive line is only a hint — the deflated-Sharpe verdict above "
        "remains the authority.</p>"
        "</section>"
    )


def _per_symbol_agg(result: BacktestResult) -> dict[str, dict]:
    """Aggregate closed trades into per-symbol {net,gross,costs,n,wins}.

    Pure and order-independent — extracted so the numbers can be unit-tested
    directly (their net must sum to the book net, their counts to the trade count).
    """
    agg: dict[str, dict] = {}
    for t in result.trades:
        a = agg.setdefault(t.symbol, {"net": 0.0, "gross": 0.0, "costs": 0.0, "n": 0, "wins": 0})
        a["net"] += t.net_pnl
        a["gross"] += t.gross_pnl
        a["costs"] += t.costs
        a["n"] += 1
        a["wins"] += 1 if t.net_pnl > 0 else 0
    return agg


def _per_symbol_section(result: BacktestResult) -> str:
    """Per-symbol net/gross/costs/trades/win% breakdown (only for multi-symbol runs),
    so a result driven by one name isn't mistaken for a broad effect."""
    if len(result.symbols) <= 1 or not result.trades:
        return ""
    agg = _per_symbol_agg(result)
    names = sorted(agg)  # deterministic ordering
    if not names:
        return ""
    chart = svg.bar_chart([agg[s]["net"] for s in names], labels=names,
                          value_fmt=_chart_money, title="Net PnL by symbol")
    head = ("<tr><th>symbol</th><th>net</th><th>gross</th><th>costs</th>"
            "<th>trades</th><th>win%</th></tr>")
    rows = []
    for s in names:
        a = agg[s]
        wr = (a["wins"] / a["n"]) if a["n"] else 0.0
        rows.append(
            "<tr>"
            f"<td>{_esc(s)}</td>"
            f'<td class="num {_cls(a["net"])}">{_signed_money(a["net"])}</td>'
            f'<td class="num {_cls(a["gross"])}">{_signed_money(a["gross"])}</td>'
            f'<td class="num neg">{_money(a["costs"])}</td>'
            f'<td class="num">{a["n"]}</td>'
            f'<td class="num">{wr:.0%}</td>'
            "</tr>"
        )
    table = ('<table class="psym"><thead>' + head + "</thead><tbody>"
             + "".join(rows) + "</tbody></table>")
    return (
        '<section class="card"><h2>Per-symbol breakdown (net PnL by symbol)</h2>'
        f"{chart}"
        f'<div class="scroll-x">{table}</div></section>'
    )


def _universe_section(universe: Mapping) -> str:
    """Render the cross-sectional universe eval (from scripts/eval_real_universe.py).

    Defensive: renders whatever standard keys are present and skips the rest so a
    schema tweak never breaks the dashboard."""
    parts: list[str] = ["<h2>Cross-sectional universe</h2>"]
    meta = []
    for k, lab in (("n_symbols", "symbols"), ("n_days", "days"),
                   ("n_trials", "trials"), ("var_sr", "var(SR)")):
        if k in universe:
            v = universe[k]
            meta.append(f"<li>{lab}<span>{_esc(v)}</span></li>")
    if meta:
        parts.append('<ul class="facts facts--wide">' + "".join(meta) + "</ul>")

    strategies = universe.get("strategies") or {}
    if isinstance(strategies, Mapping) and strategies:
        head = (
            "<tr><th>strategy</th><th>net $</th><th>Sharpe</th><th>t</th>"
            "<th>P(SR&gt;0)</th><th>DSR</th><th>OOS test Sharpe</th></tr>"
        )
        body = []
        for name, s in sorted(strategies.items()):  # sorted => deterministic order
            if not isinstance(s, Mapping):
                continue
            net = s.get("book_net", s.get("total_net", 0.0))
            dsr = s.get("dsr_vs_all_trials", s.get("dsr_vs_strategies", 0.0))
            body.append(
                "<tr>"
                f"<td>{_esc(name)}</td>"
                f'<td class="num {_cls(net)}">{_signed_money(net)}</td>'
                f'<td class="num {_cls(s.get("sharpe_ann", 0.0))}">{_fnum(s.get("sharpe_ann", 0.0))}</td>'
                f'<td class="num">{_fnum(s.get("t_stat", 0.0))}</td>'
                f'<td class="num">{_fnum(s.get("psr_vs_zero", 0.0), "{:.3f}")}</td>'
                f'<td class="num">{_fnum(dsr, "{:.3f}")}</td>'
                f'<td class="num">{_fnum(s.get("test_sharpe_ann"))}</td>'
                "</tr>"
            )
        parts.append(
            '<table class="scorecard"><thead>' + head + "</thead><tbody>"
            + "".join(body) + "</tbody></table>"
        )

    # Optional per-symbol x per-strategy Sharpe heatmap (additive key).
    ps = universe.get("per_symbol")
    if isinstance(ps, Mapping) and ps:
        strat_names = sorted(ps.keys())  # sorted => deterministic rows
        symbols: list[str] = []
        for s in strat_names:
            for sym in (ps[s] if isinstance(ps[s], Mapping) else {}):
                if sym not in symbols:
                    symbols.append(sym)
        symbols.sort()
        matrix = [
            [ps[s].get(sym) if isinstance(ps[s], Mapping) else None for sym in symbols]
            for s in strat_names
        ]
        parts.append("<h3>Per-symbol Sharpe (annualised, net)</h3>")
        parts.append(
            '<div class="scroll-x">'
            + svg.heatmap(matrix, strat_names, symbols,
                          title="Per-symbol annualised Sharpe by strategy")
            + "</div>"
        )

    best = universe.get("best_trial")
    if isinstance(best, Mapping) and best:
        parts.append(
            '<p class="muted">Best single trial of '
            f'{_esc(universe.get("n_trials", "?"))}: '
            f'<b>{_esc(best.get("strategy", "?"))}/{_esc(best.get("symbol", "?"))}</b> '
            f'Sharpe {_fnum(best.get("sharpe_ann", 0.0))}, t {_fnum(best.get("t_stat", 0.0))}, '
            f'net {_signed_money(best.get("total_net", 0.0))}, '
            f'DSR {_fnum(best.get("deflated_sharpe", 0.0), "{:.3f}")} '
            "(a best-of-N pick is expected to look good by luck — the DSR is the honest read).</p>"
        )
    return '<section class="card">' + "".join(parts) + "</section>"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def render_dashboard(
    result: BacktestResult,
    metrics: MetricsReport,
    ev: StrategyEval,
    *,
    title: str = "Intraday engine — backtest dashboard",
    generated_at: str | None = None,
    universe: Mapping | None = None,
    run_meta: Mapping | None = None,
) -> str:
    """Render the full HTML document from a result, its metrics, and its eval."""
    equity = [float(r["portfolio_value"]) for r in result.equity_curve]
    dates = [str(r["date"])[:10] for r in result.equity_curve]
    daily = daily_pnl_from_result(result)
    daily_vals = [float(v) for v in daily.to_numpy()] if len(daily) else []
    daily_dates = [str(d)[:10] for d in daily.index] if len(daily) else []
    dd = _drawdown_curve(equity)

    rm = run_meta or {}
    meta_bits = [
        f"symbols <b>{_esc(', '.join(result.symbols))}</b>",
        f"interval <b>{_esc(result.interval)}</b>",
        f"strategies <b>{_esc(rm.get('strategies', '—'))}</b>",
        f"window <b>{_esc(rm.get('start', dates[0] if dates else '?'))} "
        f"→ {_esc(rm.get('end', dates[-1] if dates else '?'))}</b>",
    ]

    equity_chart = svg.line_chart(
        equity, baseline=result.initial_capital, labels=dates,
        value_fmt=_chart_money, title="Net equity curve over the backtest window",
    )
    dd_chart = svg.area_chart(dd, labels=dates, value_fmt=lambda v: f"{v:.1%}",
                              title="Underwater drawdown (fraction below the running peak)")
    pnl_chart = svg.bar_chart(daily_vals, labels=daily_dates, value_fmt=_chart_money,
                              title="Daily net PnL by trading day")

    universe_html = _universe_section(universe) if universe else ""
    rolling_html = _rolling_sharpe_section(daily_vals, daily_dates)
    persym_html = _per_symbol_section(result)

    gen = _esc(generated_at) if generated_at else "—"

    body = f"""<h1>{_esc(title)}</h1>
<p class="sub">{' &middot; '.join(meta_bits)} &middot; generated {gen}</p>
{_banner(result)}
{_verdict_band(ev)}
{_kpi_grid(metrics, ev)}

<section class="card">
<h2>Equity curve (net of costs)</h2>
{equity_chart}
</section>

<div class="grid2">
<section class="card">
<h2>Drawdown (underwater)</h2>
{dd_chart}
</section>
<section class="card">
<h2>Daily PnL</h2>
{pnl_chart}
</section>
</div>

{rolling_html}

<section class="card">
<h2>Honesty scorecard</h2>
{_scorecard(ev)}
</section>

<section class="card">
<h2>Cost attribution</h2>
{_cost_attribution(metrics)}
</section>

<section class="card">
<h2>Exit reasons</h2>
{_exit_reasons(metrics) or '<p class="muted">No closed trades.</p>'}
</section>

{persym_html}

{universe_html}

<section class="card">
<h2>Trade blotter</h2>
<div class="scroll-y">{_trade_blotter(result)}</div>
</section>

<p class="foot">
Paper-only research artifact. Every figure is net of modelled transaction costs unless
labelled gross. The EDGE / NO-EDGE verdict is driven solely by the Deflated Sharpe Ratio
(Bailey &amp; L&oacute;pez de Prado), which penalises selection across
<code>n_trials={_esc(ev.n_trials)}</code> trials. No broker orders were placed; no live
account was modified. Charts are inline SVG with no external resources &mdash; this file
renders fully offline.
</p>"""
    return document(title, body)


def build_dashboard(
    result: BacktestResult,
    *,
    n_trials: int = 1,
    title: str = "Intraday engine — backtest dashboard",
    generated_at: str | None = None,
    universe: Mapping | None = None,
    run_meta: Mapping | None = None,
) -> str:
    """Convenience: compute the metrics + honesty eval, then render the dashboard."""
    metrics = build_report(result)
    ev = evaluate_result(result, n_trials=n_trials)
    return render_dashboard(
        result, metrics, ev, title=title, generated_at=generated_at,
        universe=universe, run_meta=run_meta,
    )
