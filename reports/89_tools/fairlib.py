"""Fair value of a 5m / 15m Up/Down side from Binance 1 s closes (the interceptor's model, reports/83):
p_up = Phi(ln(S / K) / (sigma * sqrt(tau0 + 19.5))), K = mean Binance [T-62, T-3], sigma = RMS of 10 s log returns over the
last hour per 1 s, tau0 = seconds to the closing TWAP (end - 62 - t)."""
import numpy as np, math
from statistics import NormalDist
B = np.load("bn1s.npz"); T0 = int(B["t0"])
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
PX = {a: B[s] for a, s in SYM.items() if s in B.files}
LP = {a: np.log(p) for a, p in PX.items()}
ND = NormalDist()
def has(a, t): return a in PX and 0 <= int(t) - T0 < len(PX[a]) and not math.isnan(PX[a][int(t) - T0])
def px(a, t): return float(PX[a][int(t) - T0])
def strike(a, start): return float(np.mean(PX[a][start - 62 - T0: start - 2 - T0]))
def sigma(a, t, hist=3600, step=10):
    lp = LP[a][int(t) - hist - T0: int(t) + 1 - T0: step]
    r = np.diff(lp)
    return float(np.sqrt(np.mean(r * r) / step))
def p_up(a, start, dur, t, L=0):
    tt = int(t) - L
    K = strike(a, start); S = px(a, tt); sg = sigma(a, tt)
    tau0 = max(0.0, (start + dur - 62) - tt)
    if tau0 <= 0:
        return None
    return ND.cdf(math.log(S / K) / (sg * math.sqrt(tau0 + 19.5) + 1e-12))
def mom(a, t, k):                       # Binance log return over the last k seconds, bp
    return 1e4 * (LP[a][int(t) - T0] - LP[a][int(t) - k - T0])
def fee(p): return 0.07 * p * (1 - p)
