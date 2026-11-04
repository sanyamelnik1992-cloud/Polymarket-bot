"""Fair-value model for Polymarket 5m crypto Up/Down markets with TWAP resolution.

Resolution (since 2026-08-14): Up  <=>  TWAP60(end) >= TWAP60(start)
  open  O = mean price over [T0-60, T0]   (known at T0, "price to beat")
  close C = mean price over [T1-60, T1]   (T1 = T0 + 300)

Model: log-price is a driftless Brownian motion with per-second vol sigma.
At time t with spot S:
  * t <= T1-60: log(C/S) ~ N(0, sigma^2 * ((T1-60-t) + 60/3))      (average of a BM over L has var L/3)
  * t >  T1-60: C = (A + tau * S * (1+e)) / 60,  e ~ N(0, sigma^2*tau/3), A = sum of prices already in the window
So   P(Up) = Phi( (log(S/O) - adj) / sd )  in the first case, with the analogous closed form in the second.
Optionally a Student-t replaces the normal to capture fat tails.
"""

import numpy as np
from scipy import stats

WINDOW = 300
L = 60


def _cdf(z, df=None):
    if df is None:
        return stats.norm.cdf(z)
    return stats.t.cdf(z, df)


def prob_up(t_rel, spot, open_twap, sigma, partial_sum=0.0, n_partial=0, df=None, window=WINDOW, lookback=L):
    """P(Up) at `t_rel` seconds after window start.

    spot        current price (same source/basis as open_twap)
    open_twap   price to beat
    sigma       per-second log-vol
    partial_sum sum of 1s prices already observed inside the closing TWAP window (t_rel > window-lookback)
    n_partial   number of seconds in partial_sum
    Vectorised over numpy arrays.
    """
    t_rel = np.asarray(t_rel, dtype=float)
    spot = np.asarray(spot, dtype=float)
    open_twap = np.asarray(open_twap, dtype=float)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-7)
    window = np.asarray(window, dtype=float)
    tw0 = window - lookback
    out = np.empty(np.broadcast(t_rel, spot, open_twap, sigma).shape)

    pre = t_rel <= tw0
    # --- before the closing TWAP window opens
    var_pre = sigma ** 2 * (np.maximum(tw0 - t_rel, 0) + lookback / 3.0)
    z_pre = np.log(spot / open_twap) / np.sqrt(var_pre)
    # --- inside the closing TWAP window
    ps = np.asarray(partial_sum, dtype=float)
    npart = np.asarray(n_partial, dtype=float)
    # remaining seconds are lookback - npart (== tau when sampled on whole seconds)
    rem = np.maximum(lookback - npart, 0)
    # need: ps + rem*S*(1+e) >= lookback*O   ->  e >= (lookback*O - ps)/(rem*S) - 1
    with np.errstate(divide="ignore", invalid="ignore"):
        thresh = (lookback * open_twap - ps) / np.maximum(rem * spot, 1e-12) - 1.0
        sd_in = sigma * np.sqrt(np.maximum(rem, 1e-9) / 3.0)
        z_in = -thresh / sd_in
    z_in = np.where(rem <= 0, np.where(ps >= lookback * open_twap, 50.0, -50.0), z_in)
    z = np.where(pre, z_pre, z_in)
    out[...] = _cdf(z, df)
    return out


def realized_sigma(prices, min_sigma=1e-5):
    """Per-second log-vol from a 1s price series."""
    p = np.asarray(prices, dtype=float)
    if len(p) < 10:
        return min_sigma
    r = np.diff(np.log(p))
    return max(float(np.sqrt(np.mean(r ** 2))), min_sigma)


def ewma_sigma(r, halflife=300):
    """EWMA per-second vol from 1s log returns (pandas Series), returns Series aligned to r."""
    return np.sqrt((r ** 2).ewm(halflife=halflife, min_periods=60).mean())
