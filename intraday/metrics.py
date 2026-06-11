"""Performance metrics (DESIGN.md §8; TASKS.md T0.5), over SWE performance_metrics.

Wraps ``engine.performance_metrics.calculate_performance_report`` (net Sharpe /
Sortino / max-DD / win-rate / profit-factor on a per-day equity curve, so the
hard-coded 252 annualization is correct) and adds intraday-specific honesty
metrics: gross vs net PnL, cost drag, expectancy per trade, payoff ratio, and
turnover. Every reported figure is NET OF COSTS unless explicitly labelled gross.

The report carries the data source so a SYNTHETIC run can never be mistaken for a
real-data result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from .backtest.engine import BacktestResult


@dataclass(frozen=True)
class MetricsReport:
    data_source: str
    n_days: int
    initial_capital: float
    final_equity: float
    # PnL
    gross_pnl: float
    net_pnl: float
    total_costs: float
    cost_pct_of_nav: float        # total costs / initial capital
    cost_per_trade: float         # total costs / number of trades ($)
    cost_bps_of_notional: float   # total costs / traded notional, in bps
    # returns / risk (net, from SWE on the daily equity curve)
    total_return: float
    annualized_return: float
    volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    calmar_ratio: float
    # trade stats
    total_trades: int
    win_rate: float
    profit_factor: float
    expectancy_per_trade: float   # net $/trade
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    turnover: float               # avg daily entry notional / NAV
    exit_reason_counts: dict

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        if self.data_source == "synthetic":
            tag = "  *** SYNTHETIC DATA - NOT A REAL EDGE ***"
        else:
            tag = f"  [REAL DATA: {self.data_source}]"
        lines = [
            "=" * 64,
            f" Intraday backtest - metrics report{tag}",
            "=" * 64,
            f" data source        : {self.data_source}",
            f" trading days       : {self.n_days}",
            f" initial capital    : ${self.initial_capital:,.2f}",
            f" final equity       : ${self.final_equity:,.2f}",
            "-" * 64,
            f" gross PnL          : ${self.gross_pnl:,.2f}",
            f" total costs        : ${self.total_costs:,.2f}",
            f" NET PnL            : ${self.net_pnl:,.2f}",
            f" cost / NAV         : {self.cost_pct_of_nav:.2%}",
            f" cost / trade       : ${self.cost_per_trade:,.2f}",
            f" cost (bps notional): {self.cost_bps_of_notional:.2f} bps",
            "-" * 64,
            f" total return (net) : {self.total_return:.2%}",
            f" annualized return  : {self.annualized_return:.2%}",
            f" volatility (ann.)  : {self.volatility:.2%}",
            f" Sharpe (net)       : {self.sharpe_ratio:.2f}",
            f" Sortino (net)      : {self.sortino_ratio:.2f}",
            f" max drawdown       : {self.max_drawdown:.2%}",
            f" Calmar             : {self.calmar_ratio:.2f}",
            "-" * 64,
            f" trades             : {self.total_trades}",
            f" win rate           : {self.win_rate:.1%}",
            f" profit factor      : {self.profit_factor:.2f}",
            f" expectancy / trade : ${self.expectancy_per_trade:,.2f} (net)",
            f" avg win / avg loss : ${self.avg_win:,.2f} / ${self.avg_loss:,.2f}",
            f" payoff ratio       : {self.payoff_ratio:.2f}",
            f" turnover (notional): {self.turnover:.2f}x NAV/day",
            f" exit reasons       : {self.exit_reason_counts}",
            "=" * 64,
        ]
        return "\n".join(lines)


def build_report(result: BacktestResult) -> MetricsReport:
    """Compute a :class:`MetricsReport` from a finished backtest."""
    from engine.performance_metrics import calculate_performance_report  # lazy

    rows = result.closed_trade_rows()
    # SWE's calculate_returns ignores its initial_capital argument (returns are
    # np.diff over the curve values) and calculate_max_drawdown anchors its running
    # peak at the first row, so a curve that starts at day 1's CLOSE silently drops
    # the first day's return from Sharpe/Sortino/volatility and censors any
    # drawdown from inception. Prepend the inception point so day 1 is a real
    # return and the peak starts at initial capital.
    curve = result.equity_curve
    if curve:
        inception_ts = (pd.Timestamp(curve[0]["date"]) - pd.Timedelta(days=1)).isoformat()
        curve = [
            {"date": inception_ts, "portfolio_value": result.initial_capital},
            *curve,
        ]
    swe = calculate_performance_report(
        closed_trades=rows,
        equity_curve=curve,
        initial_capital=result.initial_capital,
        risk_free_rate=result.config.gate.risk_free_rate,
    )
    # SWE annualizes with num_days = len(equity_curve); the prepended inception row
    # is day 0, not a session, so re-annualize total return over the true session
    # count (and keep Calmar consistent with it).
    n_sessions = max(result.n_days, 1)
    annualized_return = ((1.0 + swe.total_return) ** (252.0 / n_sessions)) - 1.0
    calmar_ratio = annualized_return / swe.max_drawdown if swe.max_drawdown > 0 else 0.0
    # A single session yields exactly one return once inception is included, and a
    # 1-element std(ddof=1) is NaN — which slips past SWE's empty/zero guards into
    # Sharpe/Sortino/volatility. Keep the pre-inception contract for the degenerate
    # case: risk stats on one day are 0.0, not NaN.
    if result.n_days < 2:
        volatility = sharpe_ratio = sortino_ratio = 0.0
    else:
        volatility = swe.volatility
        sharpe_ratio = swe.sharpe_ratio
        sortino_ratio = swe.sortino_ratio

    gross = sum(t.gross_pnl for t in result.trades)
    costs = sum(t.costs for t in result.trades)
    net = sum(t.net_pnl for t in result.trades)
    n = len(result.trades)
    expectancy = (net / n) if n else 0.0

    wins = [t.net_pnl for t in result.trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in result.trades if t.net_pnl < 0]
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf") if wins else 0.0

    entry_notional = sum(
        t.size * t.entry_price * t.instrument.multiplier for t in result.trades
    )
    ndays = max(result.n_days, 1)
    turnover = entry_notional / result.initial_capital / ndays
    cost_pct_of_nav = costs / result.initial_capital if result.initial_capital else 0.0
    cost_per_trade = (costs / n) if n else 0.0
    cost_bps_of_notional = (costs / entry_notional * 1e4) if entry_notional else 0.0

    reasons: dict[str, int] = {}
    for t in result.trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    return MetricsReport(
        data_source=result.data_source.value,
        n_days=result.n_days,
        initial_capital=result.initial_capital,
        final_equity=result.final_equity,
        gross_pnl=gross,
        net_pnl=net,
        total_costs=costs,
        cost_pct_of_nav=cost_pct_of_nav,
        cost_per_trade=cost_per_trade,
        cost_bps_of_notional=cost_bps_of_notional,
        total_return=swe.total_return,
        annualized_return=annualized_return,
        volatility=volatility,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        max_drawdown=swe.max_drawdown,
        calmar_ratio=calmar_ratio,
        total_trades=n,
        win_rate=swe.win_rate,
        profit_factor=swe.profit_factor,
        expectancy_per_trade=expectancy,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff,
        turnover=turnover,
        exit_reason_counts=reasons,
    )
