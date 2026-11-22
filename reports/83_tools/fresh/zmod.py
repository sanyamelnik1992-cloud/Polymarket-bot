"""Shared: Binance 1 s prices, strike and lead of a window at a decision time, z = lead / (sigma * sqrt(tau))."""
import json, numpy as np
B = np.load("bn1s.npz"); T0 = int(B["t0"])
PX = {a: B[s] for a, s in (("btc", "BTCUSDT"), ("eth", "ETHUSDT"), ("sol", "SOLUSDT"), ("xrp", "XRPUSDT"))}
LR = {a: np.diff(np.log(p), prepend=np.log(p[0])) * 1e4 for a, p in PX.items()}      # 1 s log returns, bp
LP = {a: np.log(p) for a, p in PX.items()}
def px(a, t): return PX[a][int(t) - T0]
def strike(w):
    p = PX[w["asset"]]
    if w["kind"] == "1h": return p[w["start"] - 1 - T0]
    return p[w["start"] - 62 - T0: w["start"] - 2 - T0].mean()
def final(w):
    p = PX[w["asset"]]
    if w["kind"] == "1h": return p[w["end"] - 1 - T0]
    return p[w["end"] - 62 - T0: w["end"] - 2 - T0].mean()
STEP = 1          # 1: RMS of 1 s returns; 10: RMS of 10 s returns / sqrt(10) (what the paper bot computes from Binance mids)
def sigma(a, t, n=300):
    if STEP == 1:
        r = LR[a][int(t) - n - T0: int(t) - T0]
        return float(np.sqrt(np.mean(r * r)))
    lp = LP[a][int(t) - n - T0: int(t) + 1 - T0: STEP]
    r = np.diff(lp) * 1e4
    return float(np.sqrt(np.mean(r * r) / STEP))
def tau(w, t):
    if w["kind"] == "1h": return max(1.0, w["end"] - t)
    ws = w["end"] - 62
    if t <= ws: return (ws - t) + 59 / 3.0
    k = t - ws; rem = 59 - k                    # inside the TWAP window: the remaining prints only
    return max(1.0, rem / 3.0 * (rem / 59.0))
def z_at(w, t, K=None):
    K = strike(w) if K is None else K
    s = px(w["asset"], t)
    if w["kind"] != "1h" and t > w["end"] - 62:
        # inside the TWAP window: expected TWAP = known part + current price for the rest
        p = PX[w["asset"]]; ws = w["end"] - 62; k = int(t - ws)
        known = p[ws - T0: ws + k - T0]
        exp = (known.sum() + s * (59 - k)) / 59.0
    else:
        exp = s
    lead = np.log(exp / K) * 1e4
    sg = sigma(w["asset"], t)
    return lead, sg, lead / (sg * np.sqrt(tau(w, t)) + 1e-9)
