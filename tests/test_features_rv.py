"""Tests for intraday.features.realized_vol (SWE-backed RV estimators).

Covers:
- bars_per_day for 1m/5m intervals (390 / 78 bars per RTH session);
- annualization_factor(1m) == sqrt(390);
- intraday_rv: finite-positive on a full synthetic SPY day, None for too-few bars;
- the SWE rescale relationship: intraday_rv == swe.<estimator>_vol * annualization_factor.

All values are deterministic (synthetic provider) and network-free.
"""

from __future__ import annotations

import math

import pytest

from intraday.features.realized_vol import (
    annualization_factor,
    bars_per_day,
    calendar_rv,
    intraday_rv,
)

pytestmark = pytest.mark.swe


def test_bars_per_day_1m_is_390():
    # RTH = 6.5h = 390 minutes -> 390 one-minute bars.
    assert bars_per_day("1m") == pytest.approx(390.0)


def test_bars_per_day_5m_is_78():
    # 390 minutes / 5 = 78 five-minute bars.
    assert bars_per_day("5m") == pytest.approx(78.0)


def test_bars_per_day_scales_inversely_with_interval():
    # A 1m session has exactly 5x the bars of a 5m session.
    assert bars_per_day("1m") == pytest.approx(5.0 * bars_per_day("5m"))


def test_annualization_factor_1m_is_sqrt_390():
    assert annualization_factor("1m") == pytest.approx(math.sqrt(390.0))


def test_annualization_factor_equals_sqrt_bars_per_day():
    for interval in ("1m", "5m"):
        assert annualization_factor(interval) == pytest.approx(
            math.sqrt(bars_per_day(interval))
        )


def test_intraday_rv_finite_positive_on_full_day(spy_bars):
    rv = intraday_rv(spy_bars.frame, "1m", window=30, estimator="garman_klass")
    assert rv is not None
    assert math.isfinite(rv)
    assert rv > 0.0
    # Annualized intraday vol for a liquid ETF should be a sane decimal,
    # comfortably under 1000% even with intraday rescaling.
    assert 0.0 < rv < 10.0


def test_intraday_rv_none_for_too_few_bars(spy_bars):
    # window=30 requires at least 31 bars; a 5-row frame is far too short.
    short = spy_bars.frame.head(5)
    assert len(short) == 5
    assert intraday_rv(short, "1m", window=30, estimator="garman_klass") is None


def test_intraday_rv_none_when_exactly_window_bars(spy_bars):
    # Boundary: need window+1 bars; exactly `window` is still insufficient.
    exactly_window = spy_bars.frame.head(30)
    assert len(exactly_window) == 30
    assert intraday_rv(exactly_window, "1m", window=30) is None


def test_intraday_rv_present_at_window_plus_one(spy_bars):
    # One more bar than the window crosses the threshold to a real estimate.
    just_enough = spy_bars.frame.head(31)
    assert len(just_enough) == 31
    rv = intraday_rv(just_enough, "1m", window=30, estimator="garman_klass")
    assert rv is not None
    assert math.isfinite(rv) and rv > 0.0


def test_intraday_rv_none_for_none_input():
    assert intraday_rv(None, "1m", window=30) is None


def test_intraday_rv_unknown_estimator_raises(spy_bars):
    with pytest.raises(ValueError):
        intraday_rv(spy_bars.frame, "1m", estimator="not_a_real_estimator")


def test_intraday_rv_rescale_close_to_close(spy_bars):
    # The documented invariant: intraday_rv on the full frame equals the raw
    # 252-day SWE estimator (which internally consumes tail(window+1)) times the
    # intraday annualization factor sqrt(bars_per_day).
    from engine import realized_vol as swe_rv

    window = 30
    interval = "1m"
    swe_daily = swe_rv.close_to_close_vol(spy_bars.frame, window=window)
    assert math.isfinite(swe_daily) and swe_daily > 0.0

    expected = swe_daily * annualization_factor(interval)
    got = intraday_rv(
        spy_bars.frame, interval, window=window, estimator="close_to_close"
    )
    assert got is not None
    assert got == pytest.approx(expected, rel=1e-12)


def test_intraday_rv_rescale_garman_klass(spy_bars):
    # Same rescale identity for the default OHLC estimator.
    from engine import realized_vol as swe_rv

    window = 30
    swe_daily = swe_rv.garman_klass_vol(spy_bars.frame, window=window)
    expected = swe_daily * annualization_factor("1m")
    got = intraday_rv(spy_bars.frame, "1m", window=window, estimator="garman_klass")
    assert got == pytest.approx(expected, rel=1e-12)


def test_intraday_rv_trailing_window_equals_full_prefix(spy_bars):
    """The engine feeds the RV estimator the last ``window+1`` bars, not the full
    session prefix, as an O(window) (not O(i)) per-tick optimization. That trim must
    be RESULT-IDENTICAL — every SWE estimator is tail-only. This locks it: a future
    SWE change that looked back further than window+1 would fail here rather than
    silently alter the feature."""
    frame = spy_bars.frame
    window = 30
    n = len(frame)
    for estimator in ("close_to_close", "parkinson", "garman_klass",
                      "rogers_satchell", "yang_zhang"):
        for i in (0, 29, 30, 31, 60, n - 1):  # below / at / above the window boundary
            full = intraday_rv(frame.iloc[: i + 1], "1m", window=window, estimator=estimator)
            trimmed = intraday_rv(
                frame.iloc[max(0, i - window) : i + 1], "1m", window=window, estimator=estimator
            )
            assert full == trimmed, f"{estimator} @ i={i}: full={full} trimmed={trimmed}"


def test_intraday_rv_estimators_disagree_but_same_order(spy_bars):
    # Different estimators give distinct (but same-magnitude) annualized vols.
    gk = intraday_rv(spy_bars.frame, "1m", window=30, estimator="garman_klass")
    c2c = intraday_rv(spy_bars.frame, "1m", window=30, estimator="close_to_close")
    assert gk is not None and c2c is not None
    assert gk != pytest.approx(c2c)
    # Both should be the same order of magnitude (within ~10x).
    assert 0.1 < gk / c2c < 10.0


# --------------------------------------------------------------------------- #
# calendar_rv — the calendar-clock leg (IV''s comparator)
# --------------------------------------------------------------------------- #
def test_calendar_rv_recovers_known_vol():
    """Closes constructed from i.i.d. normal log-returns of known sigma must
    recover sigma*sqrt(252) to sampling tolerance."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    daily_sigma = 0.012
    rets = rng.normal(0.0, daily_sigma, 400)
    closes = pd.Series(500.0 * np.exp(np.cumsum(rets)))
    rv = calendar_rv(closes, window=252)
    assert rv == pytest.approx(daily_sigma * math.sqrt(252.0), rel=0.10)


def test_calendar_rv_exact_for_alternating_returns():
    import numpy as np
    import pandas as pd

    # +1% / -1% alternating log returns: sd is exactly known.
    rets = np.array([0.01, -0.01] * 11)[:22]
    closes = pd.Series(100.0 * np.exp(np.cumsum(np.concatenate([[0.0], rets]))))
    rv = calendar_rv(closes, window=22)
    expected = float(pd.Series(rets).std(ddof=1)) * math.sqrt(252.0)
    assert rv == pytest.approx(expected)


def test_calendar_rv_none_when_history_short():
    import pandas as pd

    closes = pd.Series([100.0] * 21)            # needs window+1 = 22
    assert calendar_rv(closes, window=21) is None
    assert calendar_rv(None, window=21) is None
    assert calendar_rv(pd.Series(dtype=float), window=21) is None


def test_calendar_rv_refuses_junk_closes():
    import numpy as np
    import pandas as pd

    bad = pd.Series([100.0] * 10 + [0.0] + [100.0] * 11)       # non-positive
    assert calendar_rv(bad, window=21) is None
    nan = pd.Series([100.0] * 10 + [np.nan] + [100.0] * 11)
    assert calendar_rv(nan, window=21) is None


def test_calendar_rv_uses_only_trailing_window():
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(3)
    tail = pd.Series(400.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 22))))
    wild_head = pd.Series([100.0, 900.0, 50.0, 700.0])
    with_head = pd.concat([wild_head, tail], ignore_index=True)
    assert calendar_rv(with_head, window=21) == pytest.approx(
        calendar_rv(tail, window=21)
    )


# --------------------------------------------------------------------------- #
# trailing_session_closes — PIT gathering of the calendar leg''s inputs
# --------------------------------------------------------------------------- #
def _synth_provider():
    from intraday.config import EngineConfig
    from intraday.data.synthetic import SyntheticDataProvider

    cfg = EngineConfig.default()
    return SyntheticDataProvider(cfg.data, cfg.session)


def test_trailing_closes_are_strictly_prior_sessions():
    from datetime import date

    from intraday.features.realized_vol import trailing_session_closes

    day = date(2026, 5, 4)
    closes = trailing_session_closes(_synth_provider(), "SPY", day, n_sessions=10)
    assert closes is not None and len(closes) == 10
    assert all(d < day for d in closes.index)        # PIT: never day itself
    assert list(closes.index) == sorted(closes.index)


def test_trailing_closes_match_provider_bars():
    from datetime import date

    from intraday.features.realized_vol import trailing_session_closes

    prov = _synth_provider()
    day = date(2026, 5, 4)
    closes = trailing_session_closes(prov, "SPY", day, n_sessions=3)
    for d, c in closes.items():
        assert c == pytest.approx(float(prov.get_bars("SPY", d, "1m").frame["close"].iloc[-1]))


def test_trailing_closes_none_when_store_history_too_shallow(tmp_path):
    """A store-backed provider whose history is shallower than the request must
    yield None (stand aside) — never a shorter, quietly-degraded sample."""
    from datetime import date

    from intraday.contracts import DataSource
    from intraday.data.store import ParquetStore
    from intraday.data.store_provider import StoreBackedProvider
    from intraday.features.realized_vol import trailing_session_closes

    prov_synth = _synth_provider()
    store = ParquetStore(tmp_path / "store")
    for d in (date(2026, 4, 29), date(2026, 4, 30), date(2026, 5, 1)):
        import dataclasses

        bars = prov_synth.get_bars("SPY", d, "1m")
        store.write_bars(dataclasses.replace(bars, source=DataSource.YAHOO), d)
    prov = StoreBackedProvider(store, DataSource.YAHOO, symbols=["SPY"], interval="1m")

    assert trailing_session_closes(prov, "SPY", date(2026, 5, 4), n_sessions=3) is not None
    assert trailing_session_closes(prov, "SPY", date(2026, 5, 4), n_sessions=10) is None


def test_calendar_rv_present_at_window_plus_one():
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(11)
    closes = pd.Series(500.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 22))))
    rv = calendar_rv(closes, window=21)               # exactly window+1 closes
    assert rv is not None and math.isfinite(rv) and rv > 0


def test_trailing_closes_refuse_gapped_history(monkeypatch):
    """A hole in the provider calendar (e.g. the symbol-INTERSECTION calendar of
    a multi-symbol store dropping a day another symbol is missing) must yield
    None: each gap return spans 2+ trading days but would be annualized as one
    - stand aside, never a quietly-distorted vol."""
    from datetime import date

    from intraday.features.realized_vol import trailing_session_closes
    from intraday.timeutils import trading_days

    prov = _synth_provider()
    full = trailing_session_closes(prov, "SPY", date(2026, 5, 4), n_sessions=5)
    assert full is not None                            # contiguous -> fine

    real_days = trading_days(date(2026, 4, 1), date(2026, 5, 3))

    class _Gapped:
        def trading_days(self, start, end):
            days = [d for d in real_days if start <= d <= end]
            return days[:-3] + days[-2:]               # punch out one interior day

        def get_bars(self, symbol, day, interval="1m"):
            return prov.get_bars(symbol, day, interval)

    assert trailing_session_closes(_Gapped(), "SPY", date(2026, 5, 4), n_sessions=5) is None


def test_feature_row_to_dict_exports_rv_calendar():
    import pandas as pd

    from intraday.features.base import FeatureRow

    fr = FeatureRow(symbol="SPY", as_of=pd.Timestamp("2026-05-04 15:00", tz="UTC"),
                    rv=0.10, rv_calendar=0.24, atm_iv=0.20, vrp=-0.04, meta={})
    d = fr.to_dict()
    assert d["rv_calendar"] == pytest.approx(0.24)
    assert d["vrp"] == pytest.approx(-0.04)
