"""Tests for intraday/data/daily_context.py and its engine integration.

All tests are HERMETIC: network-free, no SWE disk files required.
RiskFreeCurve and VolRegimePanel are constructed directly from in-memory
pandas DataFrames (they wrap a .frame attribute).

Coverage:
- context_for(day) returns prior-session PIT values (strictly-before cutoff).
- All DailyContext fields (risk_free_rate, vix, vix_term_slope, vix_front_slope,
  vvix, skew) are populated when panels are present.
- DailyContext.is_backwardated is True when vix_term_slope > 1.
- Graceful None when a panel is absent (provider built with only a curve).
- from_swe(require=False) degrades to None panels when SWE_ROOT points at an
  empty tmp dir.
- ENGINE INTEGRATION: every FeatureRow captured by a minimal backtester run
  carries context fields when a DailyContextProvider is supplied, and all-None
  when not.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.data.daily_context import DailyContext, DailyContextProvider
from intraday.data.swe_offline import RiskFreeCurve, VolRegimePanel
from intraday.data.synthetic import SyntheticDataProvider
from intraday.features.base import FeatureRow

# --------------------------------------------------------------------------- #
# In-memory fixtures — no disk, no network, no SWE checkout required.
# --------------------------------------------------------------------------- #

# Dates: data row is 2026-01-02 (Friday); we will query for 2026-01-05 (Monday).
# Prior-session rule: data stamped on 2026-01-02 is available on 2026-01-05.
_DATA_DATE = pd.Timestamp("2026-01-02")
_QUERY_DATE = date(2026, 1, 5)   # strictly after _DATA_DATE → PIT-safe

# Same-day query (same calendar date as the data row) must NOT be visible when
# prior_session=True (the default).
_SAME_DAY_QUERY = date(2026, 1, 2)


def _make_rfc(rate: float = 0.042) -> RiskFreeCurve:
    """Build a tiny RiskFreeCurve in memory (values already as decimals)."""
    df = pd.DataFrame(
        {"rate_3m": [rate], "rate_6m": [rate + 0.005], "rate_2y": [rate + 0.01],
         "rate_10y": [rate + 0.015]},
        index=pd.DatetimeIndex([_DATA_DATE]),
    )
    df.index.name = "date"
    return RiskFreeCurve(df)


def _make_vrp(
    vix: float = 18.5,
    vix3m: float = 16.0,
    vix9d: float = 21.0,
    vvix: float = 90.0,
    skew: float = 125.0,
) -> VolRegimePanel:
    """Build a tiny VolRegimePanel in memory."""
    df = pd.DataFrame(
        {
            "vix": [vix],
            "vix3m": [vix3m],
            "vix6m": [vix3m + 0.5],
            "vix9d": [vix9d],
            "vvix": [vvix],
            "skew": [skew],
        },
        index=pd.DatetimeIndex([_DATA_DATE]),
    )
    df.index.name = "date"
    return VolRegimePanel(df)


# --------------------------------------------------------------------------- #
# 1) DailyContext dataclass basics
# --------------------------------------------------------------------------- #

class TestDailyContextDataclass:
    def test_is_backwardated_true_when_slope_gt_1(self):
        ctx = DailyContext(day=_QUERY_DATE, vix_term_slope=1.05)
        assert ctx.is_backwardated is True

    def test_is_backwardated_false_when_slope_lt_1(self):
        ctx = DailyContext(day=_QUERY_DATE, vix_term_slope=0.92)
        assert ctx.is_backwardated is False

    def test_is_backwardated_exactly_1_is_not_backwardated(self):
        ctx = DailyContext(day=_QUERY_DATE, vix_term_slope=1.0)
        assert ctx.is_backwardated is False

    def test_is_backwardated_none_when_slope_absent(self):
        ctx = DailyContext(day=_QUERY_DATE)
        assert ctx.is_backwardated is None

    def test_all_fields_none_by_default(self):
        ctx = DailyContext(day=_QUERY_DATE)
        assert ctx.risk_free_rate is None
        assert ctx.vix is None
        assert ctx.vix_term_slope is None
        assert ctx.vix_front_slope is None
        assert ctx.vvix is None
        assert ctx.skew is None


# --------------------------------------------------------------------------- #
# 2) DailyContextProvider — constructed directly
# --------------------------------------------------------------------------- #

class TestDailyContextProvider:
    def _provider(self) -> DailyContextProvider:
        return DailyContextProvider(
            risk_free_curve=_make_rfc(0.042),
            vol_regime=_make_vrp(vix=18.5, vix3m=16.0, vix9d=21.0, vvix=90.0, skew=125.0),
            rate_tenor="rate_3m",
        )

    # -- PIT: prior-session values available the NEXT day
    def test_context_for_returns_prior_session_rate(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.risk_free_rate == pytest.approx(0.042)

    def test_context_for_returns_prior_session_vix(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.vix == pytest.approx(18.5)

    def test_context_for_day_field_matches_requested_date(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.day == _QUERY_DATE

    # -- All vol-regime fields populated
    def test_vix_term_slope_is_vix_over_vix3m(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.vix_term_slope == pytest.approx(18.5 / 16.0)

    def test_vix_front_slope_is_vix9d_over_vix(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.vix_front_slope == pytest.approx(21.0 / 18.5)

    def test_vvix_populated(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.vvix == pytest.approx(90.0)

    def test_skew_populated(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.skew == pytest.approx(125.0)

    # -- Backwardation: vix(18.5) > vix3m(16.0) → term_slope > 1
    def test_is_backwardated_true_from_panel(self):
        ctx = self._provider().context_for(_QUERY_DATE)
        assert ctx.is_backwardated is True

    # -- PIT discipline: same-day data row must NOT be visible
    def test_same_day_query_returns_none_rate(self):
        """A rate stamped on 2026-01-02 must not be visible on 2026-01-02 (prior_session)."""
        ctx = self._provider().context_for(_SAME_DAY_QUERY)
        assert ctx.risk_free_rate is None

    def test_same_day_query_returns_none_vix(self):
        ctx = self._provider().context_for(_SAME_DAY_QUERY)
        assert ctx.vix is None

    # -- Contango regime (vix < vix3m)
    def test_is_backwardated_false_in_contango(self):
        provider = DailyContextProvider(
            vol_regime=_make_vrp(vix=14.0, vix3m=16.5),
        )
        ctx = provider.context_for(_QUERY_DATE)
        assert ctx.is_backwardated is False


# --------------------------------------------------------------------------- #
# 3) Graceful None when a panel is absent
# --------------------------------------------------------------------------- #

class TestPartialProvider:
    def test_curve_only_yields_vol_fields_none(self):
        provider = DailyContextProvider(risk_free_curve=_make_rfc())
        ctx = provider.context_for(_QUERY_DATE)
        assert ctx.risk_free_rate is not None
        assert ctx.vix is None
        assert ctx.vix_term_slope is None
        assert ctx.vix_front_slope is None
        assert ctx.vvix is None
        assert ctx.skew is None
        assert ctx.is_backwardated is None

    def test_vol_only_yields_rate_none(self):
        provider = DailyContextProvider(vol_regime=_make_vrp())
        ctx = provider.context_for(_QUERY_DATE)
        assert ctx.risk_free_rate is None
        assert ctx.vix == pytest.approx(18.5)

    def test_no_panels_all_none(self):
        provider = DailyContextProvider()
        ctx = provider.context_for(_QUERY_DATE)
        assert ctx.risk_free_rate is None
        assert ctx.vix is None
        assert ctx.is_backwardated is None


# --------------------------------------------------------------------------- #
# 4) from_swe degrades gracefully when SWE_ROOT is an empty tmp dir
# --------------------------------------------------------------------------- #

class TestFromSweDegrades:
    def test_require_false_returns_provider_with_none_panels(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWE_ROOT", str(tmp_path))
        provider = DailyContextProvider.from_swe(require=False)
        assert provider.risk_free_curve is None
        assert provider.vol_regime is None

    def test_require_false_context_all_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWE_ROOT", str(tmp_path))
        provider = DailyContextProvider.from_swe(require=False)
        ctx = provider.context_for(_QUERY_DATE)
        assert ctx.risk_free_rate is None
        assert ctx.vix is None
        assert ctx.is_backwardated is None

    def test_require_true_raises_on_missing_data(self, tmp_path, monkeypatch):
        from intraday.data.swe_offline import SweDataUnavailable
        monkeypatch.setenv("SWE_ROOT", str(tmp_path))
        with pytest.raises(SweDataUnavailable):
            DailyContextProvider.from_swe(require=True)


# --------------------------------------------------------------------------- #
# 5) Engine integration — FeatureRow carries context fields
# --------------------------------------------------------------------------- #

_ENGINE_DAY = date(2026, 1, 5)  # a known trading day (Monday)


class _CapturingStrategy:
    """Minimal strategy that records every FeatureRow and never proposes a trade."""

    strategy_id = "capture"
    needs_options = False
    needs_rv = False

    def __init__(self) -> None:
        self.rows: list[FeatureRow] = []

    def propose(self, fr: FeatureRow, *, config: EngineConfig):
        self.rows.append(fr)
        return None


def _run_one_day(
    daily_context: DailyContextProvider | None,
) -> list[FeatureRow]:
    """Run the engine for one day with the capturing strategy; return all FeatureRows."""
    cfg = EngineConfig.default()
    provider = SyntheticDataProvider(cfg.data, cfg.session)
    strat = _CapturingStrategy()
    bt = IntradayBacktester(
        cfg,
        provider,
        [strat],
        daily_context=daily_context,
    )
    bt.run(["SPY"], _ENGINE_DAY, _ENGINE_DAY, "1m")
    return strat.rows


class TestEngineIntegration:
    """Engine stamps DailyContext onto every FeatureRow (or leaves all-None)."""

    def _provider_with_data(self) -> DailyContextProvider:
        # Data dated 2026-01-02 is the prior-session value for 2026-01-05.
        return DailyContextProvider(
            risk_free_curve=_make_rfc(0.053),
            vol_regime=_make_vrp(vix=20.0, vix3m=18.0, vix9d=23.0, vvix=95.0, skew=130.0),
        )

    def test_every_row_has_risk_free_rate_when_provider_supplied(self):
        rows = _run_one_day(self._provider_with_data())
        assert rows, "engine must visit at least one bar"
        for fr in rows:
            assert fr.risk_free_rate == pytest.approx(0.053), (
                f"expected 0.053 at {fr.as_of}, got {fr.risk_free_rate}"
            )

    def test_risk_free_rate_constant_intraday(self):
        """The prior-session rate is PIT-stamped once for the day; all rows equal."""
        rows = _run_one_day(self._provider_with_data())
        rates = {fr.risk_free_rate for fr in rows}
        assert len(rates) == 1

    def test_every_row_has_vix_when_provider_supplied(self):
        rows = _run_one_day(self._provider_with_data())
        for fr in rows:
            assert fr.vix == pytest.approx(20.0)

    def test_every_row_has_vix_term_slope(self):
        rows = _run_one_day(self._provider_with_data())
        for fr in rows:
            assert fr.vix_term_slope == pytest.approx(20.0 / 18.0)

    def test_every_row_has_vix_front_slope(self):
        rows = _run_one_day(self._provider_with_data())
        for fr in rows:
            assert fr.vix_front_slope == pytest.approx(23.0 / 20.0)

    def test_every_row_has_vvix(self):
        rows = _run_one_day(self._provider_with_data())
        for fr in rows:
            assert fr.vvix == pytest.approx(95.0)

    def test_every_row_has_skew(self):
        rows = _run_one_day(self._provider_with_data())
        for fr in rows:
            assert fr.skew == pytest.approx(130.0)

    def test_all_context_fields_none_without_provider(self):
        rows = _run_one_day(daily_context=None)
        assert rows, "engine must visit at least one bar"
        for fr in rows:
            assert fr.risk_free_rate is None
            assert fr.vix is None
            assert fr.vix_term_slope is None
            assert fr.vix_front_slope is None
            assert fr.vvix is None
            assert fr.skew is None
