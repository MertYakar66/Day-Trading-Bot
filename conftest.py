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
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_VENDOR_SWE = _ROOT / "vendor" / "swe"

for _p in (_ROOT, _VENDOR_SWE):
    _s = str(_p)
    if _p.exists() and _s not in sys.path:
        sys.path.insert(0, _s)
