"""Model edge >= 0.10 with Binance lagged 1 s and 2 s: realized edge by UTC day, asset, price band and time before the end (5m, 15m)."""
import json, collections, math, time, numpy as np
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
res = collections.defaultdict(lambda: [0, 0.0, 0.0])
for slug, trs in MT.items():
    w = W.get(slug)
    if not w or not trs or w["kind"] == "1h": continue
    a, end, kind = w["asset"], w["end"], w["kind"]
    K = strike(w); lk = math.log(K); lp, sg = LP[a], SIG[a]
    for t, sd, o, sz, px, wal in trs:
        x = end - t
        if x <= 62 or not 0.02 < px < 0.98: continue
        i = int(t) - T0
        if i < 3610 or i >= len(lp): continue
        won = o == w["win"]
        e = ((1.0 if won else 0.0) - px - fee(px)) if sd == "B" else (px - (1.0 if won else 0.0) - fee(px))
        for L in (1, 2):
            j = i - L; s = sg[j]
            if s <= 0: continue
            z = (lp[j] - lk) / (s * math.sqrt((x + L) - 62 + 59 / 3))
            pu = Phi(z); po = pu if o == "Up" else 1 - pu
            me = (po - px) if sd == "B" else (px - po)
            if me < 0.10: continue
            keys = [("day", time.strftime("%m-%d", time.gmtime(t))), ("asset", a),
                    ("price", "<0.3" if px < 0.3 else "0.3-0.5" if px < 0.5 else "0.5-0.7" if px < 0.7 else ">=0.7"),
                    ("x", "62-120" if x < 120 else "120-180" if x < 180 else "180-240" if x < 240 else "240+"),
                    ("side", "B" if sd == "B" else "S")]
            for k in keys:
                g = res[(kind, L, k)]; g[0] += 1; g[1] += sz * px; g[2] += sz * e
for kind in ("5m", "15m"):
    for L in (1, 2):
        print(f"\n{kind}, Binance {L} s before the trade, model edge >= 10c: n / $k / realized %")
        for dim in ("day", "asset", "price", "x", "side"):
            items = sorted((k[2][1], v) for k, v in res.items() if k[0] == kind and k[1] == L and k[2][0] == dim)
            print(f"   {dim:6s}", "  ".join(f"{name}: {v[0]}/{v[1]/1e3:.0f}k/{100*v[2]/v[1]:+.1f}%" for name, v in items))
