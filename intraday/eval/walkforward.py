"""Chronological (leakage-free) splits and walk-forward windows.

Out-of-sample discipline: any parameter chosen on data must be evaluated on LATER,
unseen data. These helpers only ever split by time (never shuffle), so a test
window strictly follows its train window — no look-ahead in the evaluation itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


def chronological_split(days: list[date], train_frac: float = 0.5) -> tuple[list[date], list[date]]:
    """Split a sorted day list into (train, test) by time at ``train_frac``.

    The test window strictly follows the train window (no overlap, no shuffle).
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0, 1)")
    ds = sorted(days)
    k = int(len(ds) * train_frac)
    return ds[:k], ds[k:]


@dataclass(frozen=True)
class WalkForwardWindow:
    """One expanding/rolling fold: a train range followed by a disjoint test range."""

    fold: int
    train: list[date]
    test: list[date]


def walk_forward_windows(
    days: list[date], *, n_folds: int = 4, expanding: bool = True, min_train: int = 10
) -> list[WalkForwardWindow]:
    """Split sorted days into ``n_folds`` sequential train→test folds.

    ``expanding`` grows the train window each fold (anchored start); otherwise the
    train window rolls forward with a fixed-ish length. Each test block strictly
    follows its train block. Folds with too little train data are dropped.
    """
    ds = sorted(days)
    n = len(ds)
    if n < min_train + n_folds:
        return []
    test_size = max(1, (n - min_train) // n_folds)
    out: list[WalkForwardWindow] = []
    for f in range(n_folds):
        test_start = min_train + f * test_size
        test_end = n if f == n_folds - 1 else test_start + test_size
        if test_start >= n:
            break
        train = ds[:test_start] if expanding else ds[max(0, test_start - min_train - test_size):test_start]
        test = ds[test_start:test_end]
        if len(train) >= min_train and test:
            out.append(WalkForwardWindow(f, train, test))
    return out
