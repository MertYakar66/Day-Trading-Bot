"""Hermetic tests for :mod:`intraday.data.swe_offline`.

NETWORK-FREE. Builds a tiny fake SWE on-disk tree under pytest's ``tmp_path``
and points ``$SWE_ROOT`` at it via ``monkeypatch.setenv``.  The real 11 GB
SWE data repo is never touched and need not be present.

Each test receives a *fresh* root string so ``lru_cache`` misses and we never
read stale cached objects from a previous test's fake tree.
"""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from intraday.contracts import CHAIN_COLUMNS
from intraday.data import swe_offline as mod
from intraday.data.swe_offline import (
    IVHistory,
    RiskFreeCurve,
    SweDataUnavailable,
    VolRegimePanel,
    load_index_chain,
    load_iv_history,
    load_risk_free_curve,
    load_vol_regime,
    list_index_chain_snapshots,
)

# --------------------------------------------------------------------------- #
# Helpers — build a minimal fake SWE tree
# --------------------------------------------------------------------------- #

SNAP_DATE = date(2026, 6, 1)
SNAP_DATE_STR = "20260601"


def _make_swe_root(tmp_path: Path) -> Path:
    """Return a unique subdirectory so each test gets its own lru_cache key."""
    root = tmp_path / ("swe_" + str(id(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_treasury(root: Path) -> None:
    d = root / "data" / "bloomberg"
    d.mkdir(parents=True, exist_ok=True)
    csv = (
        "date,rate_3m,rate_6m,rate_2y,rate_10y\n"
        "2026-05-30,3.6,3.8,3.9,4.4\n"
        "2026-05-31,3.7,3.9,4.0,4.5\n"
    )
    (d / "treasury_yields.csv").write_text(csv)


def _write_vol_indices(root: Path) -> None:
    d = root / "data_processed"
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-30", "2026-05-31"]),
            "vix_close": [16.5, 17.0],
            "vix3m_close": [18.0, 18.5],
            "vix9d_close": [14.0, 14.5],
            "vvix_close": [90.0, 91.0],
            "skew_close": [140.0, 141.0],
        }
    )
    df.to_parquet(d / "vol_indices_wide.parquet", index=False)


def _write_iv_full(root: Path) -> None:
    d = root / "data" / "bloomberg"
    d.mkdir(parents=True, exist_ok=True)
    rows = [
        # Normal AAPL rows (Bloomberg style: ticker has exchange suffix)
        "2026-05-30,AAPL UW,22.0,24.0,18.0,19.0,20.0,22.0",
        "2026-05-31,AAPL UW,23.0,25.0,18.5,19.5,20.5,22.5",
        # Partial-day trap: must be excluded
        "2026-03-20,AAPL UW,30.0,32.0,25.0,26.0,27.0,28.0",
    ]
    header = "date,ticker,hist_put_imp_vol,hist_call_imp_vol,volatility_30d,volatility_60d,volatility_90d,volatility_260d"
    csv = header + "\n" + "\n".join(rows) + "\n"
    (d / "sp500_vol_iv_full.csv").write_text(csv)


def _write_index_chain(root: Path) -> None:
    d = root / "data_processed" / "theta" / "index_options_chains"
    d.mkdir(parents=True, exist_ok=True)
    # Two expiries; some iv==0 rows
    rows = []
    for expiry, strike, right, iv, oi, upx in [
        ("2026-06-20", 5500, "C", 0.20, 1000, 5475.0),
        ("2026-06-20", 5500, "P", 0.00, 800, 5475.0),   # iv==0, must be filtered
        ("2026-06-20", 5550, "C", 0.18, 1200, 5475.0),
        ("2026-06-20", 5550, "P", 0.19, 900, 5475.0),
        ("2026-07-18", 5400, "C", 0.22, 500, 5475.0),
        ("2026-07-18", 5400, "P", 0.21, 600, 5475.0),
    ]:
        rows.append(
            {
                "symbol": "SPX",
                "expiration": pd.Timestamp(expiry),
                "strike": float(strike),
                "right": right,
                "iv": float(iv),
                "open_interest": int(oi),
                "underlying_price": upx,
            }
        )
    df = pd.DataFrame(rows)
    df.to_parquet(d / f"SPX_{SNAP_DATE_STR}.parquet", index=False)


def _build_full_tree(tmp_path: Path) -> Path:
    root = _make_swe_root(tmp_path)
    _write_treasury(root)
    _write_vol_indices(root)
    _write_iv_full(root)
    _write_index_chain(root)
    return root


# --------------------------------------------------------------------------- #
# _require / missing-file errors
# --------------------------------------------------------------------------- #


def test_require_raises_on_missing_file(tmp_path):
    missing = tmp_path / "no_such" / "file.csv"
    with pytest.raises(SweDataUnavailable, match="SWE file not found"):
        mod._require(missing)


# --------------------------------------------------------------------------- #
# RiskFreeCurve / load_risk_free_curve
# --------------------------------------------------------------------------- #


class TestRiskFreeCurve:
    def _load(self, tmp_path: Path) -> RiskFreeCurve:
        root = _make_swe_root(tmp_path)
        _write_treasury(root)
        return load_risk_free_curve(str(root))

    def test_returns_risk_free_curve(self, tmp_path):
        curve = self._load(tmp_path)
        assert isinstance(curve, RiskFreeCurve)

    def test_percent_to_decimal(self, tmp_path):
        curve = self._load(tmp_path)
        # 3.7% in the CSV -> 0.037 after /100
        val = curve.rate_on(date(2026, 6, 1), "rate_3m")
        assert abs(val - 0.037) < 1e-9

    def test_result_is_decimal_not_percent(self, tmp_path):
        curve = self._load(tmp_path)
        val = curve.rate_on(date(2026, 6, 1), "rate_3m")
        # Must be well below 1.0 (not 3.7)
        assert val < 1.0

    def test_rate_on_prior_session_pit(self, tmp_path):
        """Strictly-before: querying ON 2026-05-31 returns 2026-05-30's rate."""
        curve = self._load(tmp_path)
        # prior_session=True (default): cutoff = 2026-05-31 - 1 day = 2026-05-30
        val = curve.rate_on(date(2026, 5, 31), "rate_3m", prior_session=True)
        assert abs(val - 0.036) < 1e-9  # 3.6% row

    def test_rate_on_not_prior_session(self, tmp_path):
        """prior_session=False: cutoff == day, so 2026-05-31's rate is usable."""
        curve = self._load(tmp_path)
        val = curve.rate_on(date(2026, 5, 31), "rate_3m", prior_session=False)
        assert abs(val - 0.037) < 1e-9  # 3.7% row

    def test_rate_on_raises_when_no_data_before(self, tmp_path):
        curve = self._load(tmp_path)
        with pytest.raises(SweDataUnavailable):
            curve.rate_on(date(2020, 1, 1), "rate_3m")

    def test_gt1_guard_raises(self, tmp_path):
        """Values > 100% in the CSV (would be > 1.0 after /100) must be rejected."""
        root = _make_swe_root(tmp_path)
        d = root / "data" / "bloomberg"
        d.mkdir(parents=True, exist_ok=True)
        # 200 / 100 = 2.0 > 1.0 -> guard fires
        csv = "date,rate_3m,rate_6m,rate_2y,rate_10y\n2026-05-30,200.0,3.8,3.9,4.4\n"
        (d / "treasury_yields.csv").write_text(csv)
        with pytest.raises(SweDataUnavailable, match="implausible"):
            load_risk_free_curve(str(root))

    def test_missing_file_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SweDataUnavailable):
            load_risk_free_curve(str(root))

    def test_unknown_tenor_raises(self, tmp_path):
        curve = self._load(tmp_path)
        with pytest.raises(SweDataUnavailable, match="unknown tenor"):
            curve.rate_on(date(2026, 6, 1), "rate_1y")

    def test_start_end_properties(self, tmp_path):
        curve = self._load(tmp_path)
        assert curve.start == date(2026, 5, 30)
        assert curve.end == date(2026, 5, 31)


# --------------------------------------------------------------------------- #
# VolRegimePanel / load_vol_regime
# --------------------------------------------------------------------------- #


class TestVolRegimePanel:
    def _load(self, tmp_path: Path) -> VolRegimePanel:
        root = _make_swe_root(tmp_path)
        _write_vol_indices(root)
        return load_vol_regime(str(root))

    def test_returns_vol_regime_panel(self, tmp_path):
        panel = self._load(tmp_path)
        assert isinstance(panel, VolRegimePanel)

    def test_term_slope_is_vix_over_vix3m(self, tmp_path):
        """term_slope = vix / vix3m with prior_session semantics."""
        panel = self._load(tmp_path)
        # Query on 2026-05-31 with prior_session=True -> uses 2026-05-30's row
        slope = panel.term_slope(date(2026, 5, 31))
        assert slope is not None
        assert abs(slope - 16.5 / 18.0) < 1e-9

    def test_front_slope_is_vix9d_over_vix(self, tmp_path):
        panel = self._load(tmp_path)
        slope = panel.front_slope(date(2026, 5, 31))
        assert slope is not None
        assert abs(slope - 14.0 / 16.5) < 1e-9

    def test_vvix_and_skew_prior_session(self, tmp_path):
        panel = self._load(tmp_path)
        assert panel.vvix(date(2026, 5, 31)) == pytest.approx(90.0)
        assert panel.skew(date(2026, 5, 31)) == pytest.approx(140.0)

    def test_row_asof_returns_none_before_data(self, tmp_path):
        panel = self._load(tmp_path)
        row = panel.row_asof(date(2020, 1, 1))
        assert row is None

    def test_prior_session_excludes_same_day(self, tmp_path):
        """On the very first date in the series, prior_session=True => no data."""
        panel = self._load(tmp_path)
        row = panel.row_asof(date(2026, 5, 30), prior_session=True)
        # prior_session=True: cutoff = 2026-05-29, which is before the first row
        assert row is None

    def test_prior_session_false_includes_same_day(self, tmp_path):
        panel = self._load(tmp_path)
        row = panel.row_asof(date(2026, 5, 30), prior_session=False)
        assert row is not None
        assert row["vix"] == pytest.approx(16.5)

    def test_missing_file_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SweDataUnavailable):
            load_vol_regime(str(root))

    def test_vix_method(self, tmp_path):
        panel = self._load(tmp_path)
        # prior_session=True on 2026-05-31 => 2026-05-30 row => vix=16.5
        assert panel.vix(date(2026, 5, 31)) == pytest.approx(16.5)


# --------------------------------------------------------------------------- #
# IVHistory / load_iv_history
# --------------------------------------------------------------------------- #


class TestIVHistory:
    def _load(self, tmp_path: Path, ticker: str = "AAPL") -> IVHistory:
        root = _make_swe_root(tmp_path)
        _write_iv_full(root)
        return load_iv_history(ticker, root=str(root))

    def test_returns_iv_history(self, tmp_path):
        iv = self._load(tmp_path)
        assert isinstance(iv, IVHistory)

    def test_ticker_normalized(self, tmp_path):
        """Bloomberg exchange suffix stripped; root stored as equity root."""
        iv = self._load(tmp_path)
        assert iv.ticker == "AAPL"

    def test_percent_to_decimal(self, tmp_path):
        iv = self._load(tmp_path)
        row = iv.frame.loc[iv.frame.index == pd.Timestamp("2026-05-30")]
        assert not row.empty
        # hist_call_imp_vol=24.0 in CSV -> 0.24 after /100
        assert abs(float(row["call_iv"].iloc[0]) - 0.24) < 1e-9
        # hist_put_imp_vol=22.0 -> 0.22
        assert abs(float(row["put_iv"].iloc[0]) - 0.22) < 1e-9

    def test_atm_iv_is_mean_of_call_and_put(self, tmp_path):
        iv = self._load(tmp_path)
        row = iv.frame.loc[iv.frame.index == pd.Timestamp("2026-05-30")]
        expected = (0.24 + 0.22) / 2
        assert abs(float(row["atm_iv"].iloc[0]) - expected) < 1e-9

    def test_partial_day_trap_excluded(self, tmp_path):
        iv = self._load(tmp_path)
        assert pd.Timestamp("2026-03-20") not in iv.frame.index

    def test_only_two_rows_remain(self, tmp_path):
        """3 CSV rows - 1 partial-day trap = 2 clean rows."""
        iv = self._load(tmp_path)
        assert len(iv.frame) == 2

    def test_atm_iv_asof_prior_session(self, tmp_path):
        iv = self._load(tmp_path)
        # Query on 2026-05-31, prior_session=True => uses 2026-05-30 row
        val = iv.atm_iv_asof(date(2026, 5, 31))
        assert val is not None
        assert abs(val - (0.24 + 0.22) / 2) < 1e-9

    def test_atm_iv_asof_none_before_data(self, tmp_path):
        iv = self._load(tmp_path)
        assert iv.atm_iv_asof(date(2020, 1, 1)) is None

    def test_index_ticker_raises_swe_unavailable(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_iv_full(root)
        with pytest.raises(SweDataUnavailable):
            load_iv_history("SPY", root=str(root))

    def test_spx_ticker_raises_swe_unavailable(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_iv_full(root)
        with pytest.raises(SweDataUnavailable):
            load_iv_history("SPX", root=str(root))

    def test_unknown_ticker_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_iv_full(root)
        with pytest.raises(SweDataUnavailable):
            load_iv_history("ZZZZ", root=str(root))

    def test_rv_cols_percent_to_decimal(self, tmp_path):
        iv = self._load(tmp_path)
        # volatility_30d=18.0 in CSV -> 0.18 after /100
        row = iv.frame.loc[iv.frame.index == pd.Timestamp("2026-05-30")]
        assert abs(float(row["rv_30d"].iloc[0]) - 0.18) < 1e-9

    def test_missing_file_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SweDataUnavailable):
            load_iv_history("AAPL", root=str(root))

    def test_bloomberg_suffix_stripped_on_lookup(self, tmp_path):
        """'AAPL UW' in the ticker column -> root 'AAPL', so lookup by 'AAPL' works."""
        root = _make_swe_root(tmp_path)
        _write_iv_full(root)
        iv = load_iv_history("AAPL UW", root=str(root))
        assert iv.ticker == "AAPL"


# --------------------------------------------------------------------------- #
# list_index_chain_snapshots
# --------------------------------------------------------------------------- #


class TestListIndexChainSnapshots:
    def test_returns_one_entry(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        snaps = list_index_chain_snapshots("SPX", root=str(root))
        assert len(snaps) == 1

    def test_entry_contains_date_and_path(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        snaps = list_index_chain_snapshots("SPX", root=str(root))
        d, p = snaps[0]
        assert d == SNAP_DATE
        assert p.exists()

    def test_sorted_ascending(self, tmp_path):
        """Add a second snapshot; verify list is sorted."""
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        d = root / "data_processed" / "theta" / "index_options_chains"
        # Copy the parquet as a second date (2026-07-01)
        earlier_path = d / "SPX_20260601.parquet"
        later = d / "SPX_20260701.parquet"
        import shutil
        shutil.copy(earlier_path, later)
        snaps = list_index_chain_snapshots("SPX", root=str(root))
        assert len(snaps) == 2
        assert snaps[0][0] < snaps[1][0]

    def test_missing_dir_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SweDataUnavailable):
            list_index_chain_snapshots("SPX", root=str(root))

    def test_no_matching_symbol_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        with pytest.raises(SweDataUnavailable):
            list_index_chain_snapshots("NDX", root=str(root))


# --------------------------------------------------------------------------- #
# load_index_chain
# --------------------------------------------------------------------------- #


class TestLoadIndexChain:
    def _load(self, tmp_path: Path, **kw):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        return load_index_chain("SPX", SNAP_DATE, root=str(root), **kw)

    def test_returns_tuple_chain_expiry(self, tmp_path):
        chain, expiry = self._load(tmp_path)
        from intraday.contracts import OptionChainSeries
        assert isinstance(chain, OptionChainSeries)
        assert isinstance(expiry, date)

    def test_chain_columns(self, tmp_path):
        chain, _ = self._load(tmp_path)
        assert tuple(chain.frame.columns) == CHAIN_COLUMNS

    def test_default_selects_nearest_expiry(self, tmp_path):
        """No expiry arg -> nearest (2026-06-20)."""
        _, expiry = self._load(tmp_path)
        assert expiry == date(2026, 6, 20)

    def test_explicit_expiry_selection(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        chain, expiry = load_index_chain(
            "SPX", SNAP_DATE, root=str(root), expiry=date(2026, 7, 18)
        )
        assert expiry == date(2026, 7, 18)
        assert (chain.frame["expiration"] == pd.Timestamp("2026-07-18")).all()

    def test_iv_zero_rows_filtered(self, tmp_path):
        """The iv==0 put at strike 5500 must be excluded."""
        chain, _ = self._load(tmp_path)
        assert (chain.frame["implied_vol"] > 0).all()

    def test_shape_excludes_other_expiry(self, tmp_path):
        """Default load picks nearest expiry (2026-06-20): 3 rows written,
        1 iv==0 filtered -> 2 rows remain."""
        chain, _ = self._load(tmp_path)
        # Nearest expiry had 3 rows: (5500C iv=0.2), (5500P iv=0.0 filtered), (5550C iv=0.18), (5550P iv=0.19)
        # -> 3 rows survive iv>0
        assert len(chain.frame) == 3

    def test_no_rows_from_other_expiry_in_result(self, tmp_path):
        chain, _ = self._load(tmp_path)
        unique_exp = pd.to_datetime(chain.frame["expiration"]).dt.date.unique()
        assert list(unique_exp) == [date(2026, 6, 20)]

    def test_symbol_upper(self, tmp_path):
        chain, _ = self._load(tmp_path)
        assert chain.symbol == "SPX"

    def test_missing_snapshot_date_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        with pytest.raises(SweDataUnavailable):
            load_index_chain("SPX", date(2025, 1, 1), root=str(root))

    def test_wrong_expiry_raises(self, tmp_path):
        root = _make_swe_root(tmp_path)
        _write_index_chain(root)
        with pytest.raises(SweDataUnavailable):
            load_index_chain("SPX", SNAP_DATE, root=str(root), expiry=date(2099, 1, 1))

    def test_missing_gex_column_raises(self, tmp_path):
        """A chain parquet missing required GEX columns must raise clearly."""
        root = _make_swe_root(tmp_path)
        d = root / "data_processed" / "theta" / "index_options_chains"
        d.mkdir(parents=True, exist_ok=True)
        # Write a parquet that's missing 'iv' and 'open_interest'
        df = pd.DataFrame(
            {
                "symbol": ["SPX"],
                "expiration": [pd.Timestamp("2026-06-20")],
                "strike": [5500.0],
                "right": ["C"],
                "underlying_price": [5475.0],
                # iv and open_interest deliberately omitted
            }
        )
        df.to_parquet(d / f"SPX_{SNAP_DATE_STR}.parquet", index=False)
        with pytest.raises(SweDataUnavailable, match="GEX-ready"):
            load_index_chain("SPX", SNAP_DATE, root=str(root))

    def test_chain_data_source_is_theta(self, tmp_path):
        from intraday.contracts import DataSource
        chain, _ = self._load(tmp_path)
        assert chain.source is DataSource.THETA

    def test_spot_taken_from_underlying_price(self, tmp_path):
        chain, _ = self._load(tmp_path)
        assert (chain.frame["spot"] == 5475.0).all()
