"""Is the book mispriced against a plain Binance fair value before the closing minute? For every taker trade (x > 62 s on 5m/15m,
x > 30 s on 1h): p_fair = Phi(z), z = ln(S/K) / (sigma * sqrt(tau)), sigma = rms of 10 s Binance returns over the last hour / sqrt(10),
tau = seconds to the closing TWAP + 59/3 (1h: seconds to the end). model edge = p_fair(bought outcome) - price. Realized edge by bucket."""
import json, collections, math, numpy as np
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
    SIG[a] = np.sqrt((c[n] - c[lo]) / np.maximum(n - lo, 1) / 10.0)        # per 1 s, log units
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
B = [-1, -0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10, 0.20, 1]
agg = collections.defaultdict(lambda: [0, 0.0, 0.0])        # (kind, price band, bucket) -> n, usd, edge$
rows = []
for slug, trs in MT.items():
    w = W.get(slug)
    if not w or not trs: continue
    a, end, kind = w["asset"], w["end"], w["kind"]
    K = strike(w); lk = math.log(K); lp, sg = LP[a], SIG[a]
    for t, sd, o, sz, px, wal in trs:
        x = end - t
        if (kind != "1h" and x <= 62) or (kind == "1h" and x <= 30) or not 0.02 < px < 0.98: continue
        i = int(t) - T0
        if i < 3600 or i >= len(lp): continue
        tau = (x - 62 + 59 / 3) if kind != "1h" else x
        s = sg[i]
        if s <= 0: continue
        z = (lp[i] - lk) / (s * math.sqrt(tau))
        pu = Phi(z); po = pu if o == "Up" else 1 - pu
        won = o == w["win"]
        if sd == "B":
            me = po - px; e = (1.0 if won else 0.0) - px - fee(px)
        else:
            me = px - po; e = px - (1.0 if won else 0.0) - fee(px)
        b = next(k for k in range(len(B) - 1) if B[k] <= me < B[k + 1])
        band = "0.2-0.8" if 0.2 <= px <= 0.8 else "tails"
        g = agg[(kind, band, b)]; g[0] += 1; g[1] += sz * px; g[2] += sz * e
json.dump({f"{k[0]}|{k[1]}|{k[2]}": v for k, v in agg.items()}, open("fair_agg.json", "w"))
for kind in ("5m", "15m", "1h"):
    for band in ("0.2-0.8", "tails"):
        print(f"\n{kind} {band}: model edge bucket -> trades, $ volume, realized edge % of $")
        for b in range(len(B) - 1):
            n, usd, e = agg.get((kind, band, b), [0, 0, 0])
            if n: print(f"   [{B[b]:+.2f},{B[b+1]:+.2f}): {n:8d} {usd:12.0f} {100*e/usd:+7.2f}%")
