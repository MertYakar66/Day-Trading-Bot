"""Scoped Theta OPTIONS pull — PLANNER + safety gate (does NOT pull by default).

The operator pays ~$80/mo for Theta Options-STANDARD (recurring, cancel-after), so
the historical pull MUST be tightly scoped to fit one billing month at the tier's
concurrency. This module builds that scoped work-list and prints the exact SWE
puller invocations; it NEVER opens a Theta socket itself. Executing the real pull
is a deliberate, gated operator action (see docs/OPERATOR_RUNBOOK.md).

SCOPE (DESIGN-aligned, brief §6):
  - symbols : SPX, SPY, QQQ
  - strikes : ATM ± 10
  - expiries: 0DTE + near-dated
  - window  : 2022→today (the 0DTE era the strategies need)
  - feed    : SWE vendor/swe/scripts/pull_theta_option_tape.py (trades+quotes parquet)
  - concurrency: ≤ tier limit (default 4); coordinate — the operator may be using Theta

Hard guardrails enforced here:
  - Refuses to "execute" unless BOTH ``--i-understand-this-pulls-live-theta`` AND
    env ``THETA_OPERATOR_CONFIRM=1`` are set — and even then it does not fabricate
    the SWE CLI flags: it stops with guidance so the operator wires the verified
    invocation. The default action is a dry-run PLAN only.
  - This session never runs it (no Theta — operator uses the subscription).

After the operator pulls into ``data_raw/theta/`` (or SWE's
``data_processed/theta/``), ingest into the engine store as ``DataSource.THETA``
and replay via ``StoreBackedProvider`` (see docs/REAL_DATA.md).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date

from intraday.timeutils import trading_days

DEFAULT_SYMBOLS: tuple[str, ...] = ("SPX", "SPY", "QQQ")
# The 0DTE era; the strategies (S1/S2) need this window, not pre-2022.
DEFAULT_START = date(2022, 1, 3)


@dataclass(frozen=True)
class PullScope:
    """The scoped historical-pull request (kept inside one Theta billing month)."""

    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    start: date = DEFAULT_START
    end: date = date(2026, 6, 1)
    strikes_each_side: int = 10          # ATM ± N
    expiries: str = "0dte+near"          # 0DTE plus near-dated
    max_concurrency: int = 4             # ≤ tier limit; coordinate with the operator


@dataclass(frozen=True)
class PullItem:
    """One unit of scoped work: a (symbol, session) option-tape pull."""

    symbol: str
    day: date
    strikes_each_side: int
    expiries: str

    def swe_command_template(self) -> str:
        """The recommended SWE puller invocation (flags marked for operator confirm)."""
        return (
            "python vendor/swe/scripts/pull_theta_option_tape.py "
            f"--symbol {self.symbol} --date {self.day.isoformat()} "
            f"--strikes-atm {self.strikes_each_side} --expiries {self.expiries}  "
            "# TODO(operator): confirm the SWE CLI flag names before running"
        )


def build_pull_plan(scope: PullScope, *, trading_days_fn=trading_days) -> list[PullItem]:
    """Expand a :class:`PullScope` into the per-(symbol, session) work-list."""
    days = trading_days_fn(scope.start, scope.end)
    return [
        PullItem(sym, d, scope.strikes_each_side, scope.expiries)
        for sym in scope.symbols
        for d in days
    ]


def estimate(plan: list[PullItem]) -> dict:
    """Rough size/time estimate so the operator can confirm it fits the month."""
    n = len(plan)
    # ~0.4 MB/day-contract (trades+quotes, ATM±10, 0DTE+near) and ~3 s/pull are
    # order-of-magnitude planning figures — measure the first day and recalibrate.
    return {
        "pulls": n,
        "approx_disk_mb": round(n * 0.4, 1),
        "approx_minutes_at_4x": round(n * 3.0 / 4.0 / 60.0, 1),
    }


def _render_plan(scope: PullScope, plan: list[PullItem]) -> str:
    est = estimate(plan)
    head = [
        "Scoped Theta OPTIONS pull — DRY RUN (no socket opened).",
        f"  symbols={list(scope.symbols)} window={scope.start}..{scope.end}",
        f"  strikes=ATM±{scope.strikes_each_side} expiries={scope.expiries} "
        f"concurrency={scope.max_concurrency}",
        f"  pulls={est['pulls']}  ~{est['approx_disk_mb']} MB  "
        f"~{est['approx_minutes_at_4x']} min at {scope.max_concurrency}x",
        "  (estimates are order-of-magnitude — measure day 1 and recalibrate)",
        "",
        "First / last planned invocations:",
    ]
    sample = (plan[:2] + ["..."] + plan[-2:]) if len(plan) > 4 else plan
    body = [it.swe_command_template() if isinstance(it, PullItem) else it for it in sample]
    tail = ["", "To actually pull: follow docs/OPERATOR_RUNBOOK.md (gated, operator-only)."]
    return "\n".join(head + body + tail)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Plan (and gate) the scoped Theta options pull.")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 6, 1))
    p.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--strikes-each-side", dest="strikes", type=int, default=10)
    p.add_argument("--max-concurrency", dest="concurrency", type=int, default=4)
    p.add_argument(
        "--i-understand-this-pulls-live-theta", dest="confirm_flag", action="store_true",
        help="operator confirmation flag (also needs env THETA_OPERATOR_CONFIRM=1)",
    )
    args = p.parse_args(argv)

    scope = PullScope(
        symbols=tuple(args.symbols), start=args.start, end=args.end,
        strikes_each_side=args.strikes, max_concurrency=args.concurrency,
    )
    plan = build_pull_plan(scope)

    confirmed = args.confirm_flag and os.environ.get("THETA_OPERATOR_CONFIRM") == "1"
    if not confirmed:
        print(_render_plan(scope, plan))
        return 0

    # Even when confirmed, refuse to fabricate the SWE CLI signature or open a
    # socket from this tool. The operator runs the verified invocations themselves.
    raise SystemExit(
        "Execution is intentionally not wired here. The planned commands above use "
        "PLACEHOLDER SWE CLI flags (see '# TODO(operator)'). Verify the real flags "
        "in vendor/swe/scripts/pull_theta_option_tape.py and run them per "
        "docs/OPERATOR_RUNBOOK.md, respecting Theta concurrency."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
