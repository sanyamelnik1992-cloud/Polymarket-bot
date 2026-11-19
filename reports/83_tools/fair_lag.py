"""fair.py with the Binance price LAGGED by L seconds (no look-ahead: the close of second t-L), several L. 5m/15m/1h, all prices."""
import json, collections, math, sys, numpy as np
from zmod import PX, T0, strike
W = {w["slug"]: w for w in json.load(open("windows.json"))}
MT = json.load(open("mtrades.json"))
def fee(p): return 0.07 * p * (1 - p)
LP, SIG = {}, {}
for a, p in PX.items():
    lp = np.log(p); LP[a] = lp
    r10 = np.zeros_like(lp); r10[10:] = (lp[10:] - lp[:-10])
    c = np.concatenate([[0.0], np.cumsum(r10 * r10)])
    n = np.arange(len(lp)); lo = np.maximum(n - 3600, 0)
    SIG[a] = np.sqrt((c[n] - c[lo]) / np.maximum(n - lo, 1) / 10.0)
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
LAGS = [0, 1, 2, 3, 5]
B = [-1, -0.05, 0.0, 0.05, 0.10, 0.20, 1]
agg = collections.defaultdict(lambda: [0, 0.0, 0.0])
for slug, trs in MT.items():
    w = W.get(slug)
    if not w or not trs: continue
    a, end, kind = w["asset"], w["end"], w["kind"]
    K = strike(w); lk = math.log(K); lp, sg = LP[a], SIG[a]
    for t, sd, o, sz, px, wal in trs:
        x = end - t
        if (kind != "1h" and x <= 62) or (kind == "1h" and x <= 30) or not 0.02 < px < 0.98: continue
        i = int(t) - T0
        if i < 3610 or i >= len(lp): continue
        won = o == w["win"]
        e = ((1.0 if won else 0.0) - px - fee(px)) if sd == "B" else (px - (1.0 if won else 0.0) - fee(px))
        for L in LAGS:
            j = i - L
            tau = ((x + L) - 62 + 59 / 3) if kind != "1h" else (x + L)
            s = sg[j]
            if s <= 0: continue
            z = (lp[j] - lk) / (s * math.sqrt(tau))
            pu = Phi(z); po = pu if o == "Up" else 1 - pu
            me = (po - px) if sd == "B" else (px - po)
            b = next(k for k in range(len(B) - 1) if B[k] <= me < B[k + 1])
            g = agg[(kind, L, b)]; g[0] += 1; g[1] += sz * px; g[2] += sz * e
for kind in ("5m", "15m", "1h"):
    print(f"\n{kind}: realized edge % of $ by model-edge bucket, Binance lagged L s   (volume $k in brackets)")
    print("   bucket        " + "".join(f"     L={L}s          " for L in LAGS))
    for b in range(len(B) - 1):
        cells = []
        for L in LAGS:
            n, usd, e = agg.get((kind, L, b), [0, 0, 0])
            cells.append(f"{100*e/usd:+7.2f}% ({usd/1e3:7.0f})" if usd else " " * 19)
        print(f"   [{B[b]:+.2f},{B[b+1]:+.2f}) " + "  ".join(cells))
