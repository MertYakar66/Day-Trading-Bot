"""Make the read-only SWE dependency importable from any entry point.

Importing :mod:`intraday` runs this module, which appends ``<repo>/vendor/swe``
to ``sys.path`` so the reused quant modules (``engine.*``, ``backtests.*``)
resolve without requiring ``PYTHONPATH`` to be set by hand. This is the
non-pytest half of TASKS.md T0.0 (pytest gets the same paths via
``pyproject.toml``'s ``pythonpath`` and ``conftest.py``).

Guardrails honored here:
- We only *prepend a path*; we never import or call any SWE module, so this has
  no side effects and never touches the network / Theta Terminal.
- The SWE tree stays read-only; nothing here writes to it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# intraday/_vendor.py -> intraday/ -> <repo root>
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
VENDOR_SWE: Path = REPO_ROOT / "vendor" / "swe"


def ensure_swe_on_path() -> Path:
    """Idempotently put ``vendor/swe`` on ``sys.path``. Returns its path."""
    s = str(VENDOR_SWE)
    if VENDOR_SWE.exists() and s not in sys.path:
        sys.path.insert(0, s)
    return VENDOR_SWE


def swe_available() -> bool:
    """True if the SWE submodule appears checked out (has ``engine/``)."""
    return (VENDOR_SWE / "engine").is_dir()


ensure_swe_on_path()
