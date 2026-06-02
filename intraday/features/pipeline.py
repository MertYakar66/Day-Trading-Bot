"""Feature assembly: a PIT-stamped :class:`FeatureRow` at a decision time.

The pipeline pre-computes the causal session series once (VWAP frame, opening
range) so the backtest can sample them cheaply at every tick, then layers on the
point-in-time option features (OFI, VRP, GEX) when a chain/tape is supplied.

All sampling goes through :func:`latest_value` / ``available_at`` so no value is
read before it has arrived — the no-look-ahead invariant holds end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..config import EngineConfig
from ..contracts import BarSeries, OptionChainSeries, OptionTape
from ..logging_config import get_logger
from ..timeutils import session_bounds_utc
from .base import FeatureRow, latest_value
from .gex import gamma_structure_at
from .ofi import ofi_at
from .opening_range import OpeningRange, opening_range
from .vrp import vrp_at
from .vwap import vwap_frame

logger = get_logger(__name__)


@dataclass(frozen=True)
class SessionFeatures:
    """Pre-computed causal session series (sampled per tick by the backtest)."""

    symbol: str
    vwap: pd.DataFrame              # causal vwap/vwap_sigma/vwap_dev_sigma
    opening_range: OpeningRange | None
    close: pd.Series               # bar close series (causal)
    bar_latency: pd.Timedelta


class FeaturePipeline:
    """Builds session features and per-tick :class:`FeatureRow`s."""

    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        orb_minutes: int = 15,
        ofi_lookback_min: int = 5,
        rv_window: int = 30,
        rv_estimator: str = "garman_klass",
    ) -> None:
        self.config = config or EngineConfig.default()
        self.orb_minutes = orb_minutes
        self.ofi_lookback = pd.Timedelta(minutes=ofi_lookback_min)
        self.rv_window = rv_window
        self.rv_estimator = rv_estimator

    def precompute(self, bars: BarSeries, day: date) -> SessionFeatures:
        open_utc, _ = session_bounds_utc(day, self.config.session)
        frame = bars.frame
        return SessionFeatures(
            symbol=bars.symbol,
            vwap=vwap_frame(frame),
            opening_range=opening_range(frame, open_utc, self.orb_minutes),
            close=frame["close"],
            bar_latency=bars.latency,
        )

    def row(
        self,
        symbol: str,
        day: date,
        as_of: pd.Timestamp,
        *,
        bars: BarSeries,
        session: SessionFeatures | None = None,
        chain: OptionChainSeries | None = None,
        tape: OptionTape | None = None,
        interval: str = "1m",
    ) -> FeatureRow:
        sf = session or self.precompute(bars, day)
        lat = sf.bar_latency

        last_price = latest_value(sf.close, as_of, lat)
        vwap = latest_value(sf.vwap["vwap"], as_of, lat)
        vwap_sigma = latest_value(sf.vwap["vwap_sigma"], as_of, lat)
        vwap_dev = latest_value(sf.vwap["vwap_dev_sigma"], as_of, lat)

        orb_high = orb_low = orb_vol = None
        if sf.opening_range is not None and sf.opening_range.is_known_at(as_of, lat):
            orb_high = sf.opening_range.high
            orb_low = sf.opening_range.low
            orb_vol = sf.opening_range.volume

        ofi = ofi_at(tape, as_of, self.ofi_lookback) if tape is not None else None

        atm_iv = rv = vrp_val = None
        gex_total = gamma_regime = flip_level = flip_dist = ncw = npw = None
        if chain is not None:
            pit_bars = bars.available_at(as_of)
            try:
                v = vrp_at(
                    chain, pit_bars, as_of, interval,
                    window=self.rv_window, estimator=self.rv_estimator,
                    session=self.config.session,
                )
                atm_iv, rv, vrp_val = v.atm_iv, v.rv, v.vrp
            except ImportError:  # pragma: no cover - SWE always present in runs
                logger.debug("VRP skipped: engine.realized_vol unavailable")
            try:
                gs = gamma_structure_at(
                    chain, as_of, expiry=day, ticker=symbol,
                    risk_free_rate=self.config.gate.risk_free_rate,
                )
                if gs is not None:
                    gex_total = gs.gex_total
                    gamma_regime = gs.regime
                    flip_level = gs.flip_level
                    flip_dist = gs.flip_distance_pct
                    ncw, npw = gs.nearest_call_wall, gs.nearest_put_wall
            except ImportError:  # pragma: no cover
                logger.debug("GEX skipped: engine.dealer_positioning unavailable")

        return FeatureRow(
            symbol=symbol,
            as_of=pd.Timestamp(as_of),
            last_price=None if last_price is None else float(last_price),
            vwap=None if vwap is None else float(vwap),
            vwap_sigma=None if vwap_sigma is None else float(vwap_sigma),
            vwap_dev_sigma=None if vwap_dev is None or pd.isna(vwap_dev) else float(vwap_dev),
            orb_high=orb_high,
            orb_low=orb_low,
            orb_volume=orb_vol,
            ofi=ofi,
            rv=rv,
            atm_iv=atm_iv,
            vrp=vrp_val,
            gex_total=gex_total,
            gamma_regime=gamma_regime,
            flip_level=flip_level,
            flip_distance_pct=flip_dist,
            nearest_call_wall=ncw,
            nearest_put_wall=npw,
            meta={},
        )
