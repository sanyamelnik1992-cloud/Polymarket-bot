"""Fair value of the Up token: TWAP model on the Chainlink price (resolution source), nowcast with Binance.

spot_now = last Chainlink print * (Binance mid now / Binance mid at the time of that print)
P(Up)    = pmbot.model.prob_up(rel, spot_now, strike, sigma, partial closing-TWAP sum from Chainlink seconds)
"""
from dataclasses import dataclass

from ..model import prob_up
from .state import AssetPrices, Window

LOOKBACK = 60


@dataclass
class Fair:
    p_up: float
    spot: float
    sigma: float
    rel: float
    cl_age_ms: int
    n_partial: int


def fair_value(w: Window, px: AssetPrices, now_ms: int, vol_mult=1.0, t_df=4.0) -> Fair | None:
    if w.strike is None or px.cl_last is None:
        return None
    sigma = px.sigma()
    bn_now = px.bn_mid()
    if not sigma or not bn_now:
        return None
    cl_value, cl_ts, cl_rx = px.cl_last
    # Binance mid at (approximately) the oracle timestamp of the Chainlink print
    bn_then = px.bn_mid_at(cl_ts)
    if not bn_then:
        return None
    spot = cl_value * (bn_now / bn_then)
    rel = now_ms / 1000.0 - w.start
    tw0 = w.end - LOOKBACK
    upto = min(cl_ts // 1000 + 1, w.end)
    if upto > tw0:
        psum, npart = px.cl_partial(tw0, upto)
    else:
        psum, npart = 0.0, 0
    p = float(prob_up(rel, spot, w.strike, sigma * vol_mult, psum, npart, df=t_df, window=w.end - w.start))
    return Fair(p_up=p, spot=spot, sigma=sigma, rel=rel, cl_age_ms=now_ms - cl_rx, n_partial=npart)
