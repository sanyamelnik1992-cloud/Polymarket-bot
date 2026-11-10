"""TWAP-lock model for the last minute of a 5m Up/Down window (reports/44_twap_lock.md).

Resolution: Up  <=>  C >= K, where C is the mean of the 60 Chainlink prints whose RTDS timestamps fall in
[T+238, T+297] and K (price to beat) is the same mean over [T-62, T-3] (checked on 600 windows against Gamma's
priceToBeat: median error 0.000 bp, outcome reproduced in 444/445 windows).

At a moment inside the closing window, n prints are known (sum A). Prints that already happened but did not reach us
(RTDS delivers ~1.4 s late) are nowcast with Binance; the remaining m future prints are random. With S the nowcast
spot, Up  <=>  X_m >= thr = (60 K - A') / (m S) - 1, where X_m is the mean return of the next m prints. The
probability comes from an EMPIRICAL table of Y_m = X_m / (sigma * sqrt(v_m)) built from 37 h of Chainlink prints
(tails are ~3x fatter than Gaussian: the 0.1% quantile is about -10 sigma), with sigma = EWMA (half-life 900 s) of
1 s Chainlink log returns and v_m = (m+1)(2m+1)/(6m) the random-walk variance factor of an m-print average.
"""
import math
import os

import numpy as np

TW_LO, TW_HI = 238, 297           # closing TWAP: RTDS print timestamps (s) in [T+238, T+297]
OPEN_LO, OPEN_HI = -62, -3        # strike: [T-62, T-3]
M = 60
FLOOR = 1e-4                      # never claim more than 99.99% (overlapping samples, ~2 days of data)
DEFAULT_TABLES = os.path.join(os.path.dirname(__file__), "..", "config", "lock_tables.npz")


def vfac(m):
    m = max(int(m), 1)
    return (m + 1) * (2 * m + 1) / (6 * m)


class Tables:
    """Survival function P(Y_m >= y) per asset and m on a fixed y grid (built by scripts/build_lock_tables.py)."""

    def __init__(self, path=DEFAULT_TABLES):
        z = np.load(path, allow_pickle=False)
        self.y = z["y"]
        self.assets = [str(a) for a in z["assets"]]
        self.s = {a: z[f"s_{a}"] for a in self.assets}     # shape (60, len(y)); row m-1

    def surv(self, asset, m, y):
        if not math.isfinite(y):
            return FLOOR if y > 0 else 1 - FLOOR
        tab = self.s.get(asset)
        if tab is None:
            tab = self.s[self.assets[0]]
        m = min(max(int(m), 1), M)
        p = float(np.interp(y, self.y, tab[m - 1], left=1.0, right=0.0))
        return min(max(p, FLOOR), 1 - FLOOR)


def build_tables(series_by_asset, halflife=900, y_grid=None):
    """series_by_asset: asset -> 1 s price array (gaps forward-filled). Returns the npz payload dict."""
    y_grid = np.round(np.arange(-40, 40.0001, 0.05), 4) if y_grid is None else y_grid
    out = {"y": y_grid, "assets": np.array(sorted(series_by_asset))}
    for a, v in series_by_asset.items():
        v = np.asarray(v, dtype=float)
        lr = np.diff(np.log(v), prepend=np.nan)
        r2 = np.nan_to_num(lr ** 2, nan=0.0)
        # pandas ewm(halflife, adjust=True, min_periods=120) on squared returns
        alpha = 1 - 0.5 ** (1 / halflife)
        num = den = 0.0
        sig = np.full(len(v), np.nan)
        for i in range(len(v)):
            if i == 0:
                continue
            num = num * (1 - alpha) + r2[i]
            den = den * (1 - alpha) + 1.0
            if i >= 120:
                sig[i] = math.sqrt(num / den)
        cs = np.concatenate([[0.0], np.cumsum(v)])
        n = len(v)
        surv = np.zeros((M, len(y_grid)), dtype=np.float32)
        for m in range(1, M + 1):
            i = np.arange(0, n - m)
            fut = (cs[i + m + 1] - cs[i + 1]) / m
            Y = (fut / v[i] - 1.0) / (sig[i] * math.sqrt(vfac(m)))
            Y = np.sort(Y[np.isfinite(Y)])
            k = len(Y) - np.searchsorted(Y, y_grid, side="left")
            surv[m - 1] = k / len(Y)
        out[f"s_{a}"] = surv
    return out


class ClSigma:
    """EWMA of squared 1 s Chainlink log returns (half-life 900 s, like the tables), gaps forward-filled."""

    def __init__(self, halflife=900.0, min_n=120):
        self.alpha = 1 - 0.5 ** (1 / halflife)
        self.num = self.den = 0.0
        self.n = 0
        self.min_n = min_n
        self.last_sec = None
        self.last_v = None

    def on_print(self, sec, v):
        if v is None or v <= 0:
            return
        if self.last_sec is None:
            self.last_sec, self.last_v = sec, v
            return
        if sec <= self.last_sec:
            return
        for _ in range(min(sec - self.last_sec - 1, 120)):      # forward-filled seconds = zero returns
            self._add(0.0)
        self._add(math.log(v / self.last_v) ** 2)
        self.last_sec, self.last_v = sec, v

    def _add(self, r2):
        self.num = self.num * (1 - self.alpha) + r2
        self.den = self.den * (1 - self.alpha) + 1.0
        self.n += 1

    def sigma(self):
        if self.n < self.min_n or self.den <= 0:
            return None
        return math.sqrt(self.num / self.den)


def mean_prints(cl_by_sec, first_sec, max_missing=3):
    """Mean of the 60 prints of [first_sec, first_sec + 59] and the number of missing seconds. RTDS drops ~1% of the
    seconds (a third of the windows miss one): a missing second takes the previous print (checked against priceToBeat
    on 2 days: with 1-3 missing seconds the median error is 0.001-0.004 bp, max 0.24 bp). (None, missing) if more
    than `max_missing` are missing or there is no earlier print to fill from."""
    last = None
    for s in range(first_sec - 10, first_sec):
        last = cl_by_sec.get(s, last)
    xs, miss = [], 0
    for s in range(first_sec, first_sec + M):
        v = cl_by_sec.get(s)
        if v is None:
            miss += 1
            v = last
        if v is None:
            return None, miss
        xs.append(v)
        last = v
    if miss > max_missing:
        return None, miss
    return sum(xs) / len(xs), miss


def strike_from_prints(cl_by_sec, T, max_missing=3):
    """Price to beat from received prints: mean over [T-62, T-3], gaps forward-filled (see mean_prints).
    None if more than `max_missing` are missing."""
    return mean_prints(cl_by_sec, T + OPEN_LO, max_missing)[0]


def close_from_prints(cl_by_sec, T, max_missing=3):
    """Closing TWAP from received prints: (mean over [T+238, T+297] with gaps forward-filled, missing seconds)."""
    return mean_prints(cl_by_sec, T + TW_LO, max_missing)


def lock_state(cl_by_sec, last_ts_sec, cl_last_v, T, K, now_s, bn_now=None, bn_at_last=None):
    """Known part of the closing TWAP at `now_s`.

    Returns dict(A, n, A_nc, m_nc, S_nc, m_cl, S_cl): sums/counts without the nowcast (only received prints, the
    rest random from the last received print) and with the nowcast (prints already in the past are set to S_nc)."""
    secs = range(T + TW_LO, T + TW_HI + 1)
    known = [cl_by_sec[s] for s in secs if s in cl_by_sec]
    A, n = sum(known), len(known)
    S_cl = cl_last_v
    S_nc = cl_last_v * (bn_now / bn_at_last) if bn_now and bn_at_last else cl_last_v
    first_unrecv = max(last_ts_sec + 1, T + TW_LO)
    past_hi = min(int(math.floor(now_s)) - 1, T + TW_HI)
    p_past = max(0, min(past_hi - first_unrecv + 1, M - n))
    return {"A": A, "n": n, "S_cl": S_cl, "m_cl": M - n,
            "A_nc": A + p_past * S_nc, "m_nc": M - n - p_past, "S_nc": S_nc, "p_past": p_past}


def prob_up(tables, asset, A, m, S, K, sigma):
    """P(C >= K) given the known sum A, m random future prints from S, empirical tails."""
    if m <= 0:
        return 1 - FLOOR if A >= M * K else FLOOR
    if not sigma or sigma <= 0 or not S or S <= 0:
        return 0.5
    thr = (M * K - A) / (m * S) - 1.0
    y = thr / (sigma * math.sqrt(vfac(m)))
    return tables.surv(asset, m, y)


def expected_dist_bp(A, m, S, K):
    """Expected TWAP minus strike if the price stays at S, in bp of K."""
    return ((A + m * S) / M / K - 1) * 1e4
