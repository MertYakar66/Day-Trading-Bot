"""Root pytest/conftest bootstrap for the intraday engine.

Ensures the read-only Smart Wheel Engine (SWE) dependency at ``vendor/swe`` is
importable (``import engine.*`` / ``import backtests.*``) and that this repo is
importable (``import intraday.*``) regardless of how pytest is invoked. This is
the runtime half of TASKS.md T0.0 (``pyproject.toml [tool.pytest.ini_options]``
``pythonpath`` is the declarative half; this file is belt-and-suspenders so that
ad-hoc ``pytest path/to/test.py`` from any cwd still resolves the dependency).

No network, no Theta: importing the SWE quant modules only defines classes; it
never opens a socket. Tests in this suite are network-free by construction.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
_VENDOR_SWE = _ROOT / "vendor" / "swe"

for _p in (_ROOT, _VENDOR_SWE):
    _s = str(_p)
    if _p.exists() and _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture
def make_ibkr_payload():
    """Factory building a deterministic IBKR ``get_price_history``-shaped payload.

    Bars are **start-labelled** (as IBKR returns them) across one RTH session.
    Options:
      - ``interval`` engine interval ("5m"/"1m"/…); ``base`` anchor price;
      - ``n_bars`` truncate to the first N start labels (partial session);
      - ``drop`` indices to omit (simulate missing/gapped bars);
      - ``security`` "STK" includes volume, "IND" omits the volume key entirely;
      - ``amp``/``wiggle`` shape the price path (mean-reverting sine + step noise).
    """
    import numpy as np
    import pandas as pd

    from intraday.timeutils import parse_interval, session_bounds_utc

    def _make(
        day: date,
        interval: str = "5m",
        *,
        base: float = 750.0,
        n_bars: int | None = None,
        drop: tuple[int, ...] = (),
        security: str = "STK",
        amp: float = 1.5,
        wiggle: float = 0.05,
    ) -> dict:
        open_utc, close_utc = session_bounds_utc(day)
        step = parse_interval(interval)
        starts = pd.date_range(start=open_utc, end=close_utc - step, freq=step)
        if n_bars is not None:
            starts = starts[:n_bars]
        n = len(starts)
        # Deterministic mean-reverting-ish path (no RNG → reproducible across procs).
        t = np.arange(n)
        mid = base + amp * np.sin(2.0 * np.pi * t / max(n, 1) * 3.0)
        mid = mid + wiggle * ((t % 7) - 3.0)
        op = mid.copy()
        cl = np.roll(mid, -1)
        cl[-1] = mid[-1]
        hi = np.maximum(op, cl) + 0.10
        lo = np.minimum(op, cl) - 0.10
        vol = (1000.0 + (t % 11) * 137.0)

        keep = [i for i in range(n) if i not in set(drop)]
        iso = [pd.Timestamp(starts[i]).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ") for i in keep]
        payload = {
            "chart_step": int(step.total_seconds()),
            "chart_start": pd.Timestamp(open_utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chart_end": pd.Timestamp(close_utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Last",
            "time": iso,
            "open": [round(float(op[i]), 2) for i in keep],
            "high": [round(float(hi[i]), 2) for i in keep],
            "low": [round(float(lo[i]), 2) for i in keep],
            "close": [round(float(cl[i]), 2) for i in keep],
        }
        if security != "IND":
            payload["volume"] = [float(vol[i]) for i in keep]
        return payload

    return _make
