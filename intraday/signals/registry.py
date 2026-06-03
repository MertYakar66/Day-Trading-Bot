"""Single source of truth for the strategy catalogue.

One registry maps each strategy key (``s1``..``s5``) to how it is built and how it
is described, so the CLI's ``--strategy`` choices, its ``build`` helper, its error
messages, and the ``strategies`` subcommand all agree and never drift apart. Adding
a strategy here wires it into every one of those surfaces at once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .base import Strategy
from .s1_gamma_regime import S1GammaRegime
from .s2_zerodte_vrp import S2ZeroDteVrp
from .s3_vwap_orb import S3VwapOrb
from .s4_orb_breakout import S4OpeningRangeBreakout
from .s5_vwap_momentum import S5VwapMomentum


@dataclass(frozen=True)
class StrategyInfo:
    """Catalogue entry: how to build a strategy and how to describe it.

    ``factory`` accepts the full common knob bag (``edge``/``entry_z``/``stop_k``)
    by keyword and uses only what it needs, so a single call site builds any
    strategy. ``needs_options`` flags the (slower) chain/tape-dependent strategies.
    """

    key: str
    title: str          # short name, e.g. "VWAP reversion"
    summary: str        # one-line description
    needs_options: bool
    factory: Callable[..., Strategy]


# Ordered so listings read s1..s5.
STRATEGIES: dict[str, StrategyInfo] = {
    "s1": StrategyInfo(
        "s1", "Gamma regime",
        "Dealer-gamma-conditioned reversion/trend on the underlying (uses options: GEX/flip/walls + OFI).",
        True, lambda *, edge, entry_z, stop_k: S1GammaRegime(edge=edge),
    ),
    "s2": StrategyInfo(
        "s2", "0DTE VRP iron condor",
        "Defined-risk short iron condor gated by VRP-rich + positive net GEX (uses options).",
        True, lambda *, edge, entry_z, stop_k: S2ZeroDteVrp(),
    ),
    "s3": StrategyInfo(
        "s3", "VWAP reversion",
        "Fade an >= entry_z-sigma stretch from session VWAP back toward it (underlying-only control).",
        False, lambda *, edge, entry_z, stop_k: S3VwapOrb(entry_z=entry_z, stop_k=stop_k, edge=edge),
    ),
    "s4": StrategyInfo(
        "s4", "ORB breakout",
        "Take a clean break of the opening range as momentum (underlying-only).",
        False, lambda *, edge, entry_z, stop_k: S4OpeningRangeBreakout(edge=edge),
    ),
    "s5": StrategyInfo(
        "s5", "VWAP momentum",
        "Ride an >= entry_z-sigma stretch from session VWAP (the momentum mirror of s3; underlying-only).",
        False, lambda *, edge, entry_z, stop_k: S5VwapMomentum(entry_z=entry_z, stop_k=stop_k, edge=edge),
    ),
}

STRATEGY_KEYS: tuple[str, ...] = tuple(STRATEGIES)


class UnknownStrategyError(ValueError):
    """Raised when a strategy key is not in the registry."""


def build_strategy(key: str, *, edge: float, entry_z: float, stop_k: float) -> Strategy:
    """Build one strategy by key from the common knob bag."""
    info = STRATEGIES.get(key)
    if info is None:
        raise UnknownStrategyError(
            f"unknown strategy {key!r}; choose from "
            + ", ".join(f"{k} ({STRATEGIES[k].title})" for k in STRATEGY_KEYS)
        )
    return info.factory(edge=edge, entry_z=entry_z, stop_k=stop_k)


def build_strategies(
    keys: list[str], *, edge: float, entry_z: float, stop_k: float
) -> list[Strategy]:
    """Build a list of strategies by key (preserving order)."""
    return [build_strategy(k, edge=edge, entry_z=entry_z, stop_k=stop_k) for k in keys]
