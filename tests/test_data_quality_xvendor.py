"""Cross-vendor data-quality / consistency tests (IBKR vs Yahoo).

Two independent real vendors are a strong integrity check. This file locks the
*structural* property as a committed unit test: given identical underlying bars,
the IBKR and Yahoo payload mappers must produce **identical canonical-grid frames**
(only the provenance differs). The remap is vendor-agnostic by construction (both
delegate to intraday.data._remap), so a divergence would signal a parser bug.

Real-data corroboration (recorded for the report; not a committed assertion since
the captured data is gitignored): on 12 overlapping SPY/QQQ sessions (2026-05-14..
06-01), IBKR (ARCA) vs Yahoo (consolidated) 5-min closes agreed to mean 0.12 /
0.20 bps and max 1.3 / 2.0 bps over 936 bars each — sub-2bps, as expected from
venue/rounding differences.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from intraday.contracts import DataSource
from intraday.data.ibkr import bars_by_day as ibkr_bars_by_day
from intraday.data.yahoo import bars_by_day as yahoo_bars_by_day

DAY = date(2026, 6, 1)


def test_ibkr_and_yahoo_remap_agree(make_ibkr_payload, make_yahoo_payload):
    """Identical underlying bars → identical canonical frames from both vendors."""
    # Identical path inputs to both factories (their defaults differ on purpose).
    kw = dict(base=500.0, amp=1.0, wiggle=0.05)
    ib = ibkr_bars_by_day("SPY", make_ibkr_payload(DAY, "5m", **kw), "5m")[DAY]
    yh = yahoo_bars_by_day("SPY", make_yahoo_payload(DAY, "5m", **kw), "5m")[DAY]
    # Same canonical close grid, same OHLCV (to sub-cent rounding), different source.
    pd.testing.assert_index_equal(ib.frame.index, yh.frame.index)
    pd.testing.assert_frame_equal(ib.frame, yh.frame, check_exact=False, atol=0.02)
    assert ib.source is DataSource.IBKR
    assert yh.source is DataSource.YAHOO


def test_both_vendors_skip_leading_gap_identically(make_ibkr_payload, make_yahoo_payload):
    """The no-look-ahead leading-gap skip is identical across vendors."""
    assert ibkr_bars_by_day("SPY", make_ibkr_payload(DAY, "5m", drop=(0,)), "5m") == {}
    assert yahoo_bars_by_day("SPY", make_yahoo_payload(DAY, "5m", drop=(0,)), "5m") == {}
