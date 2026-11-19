"""Every taker trade of 13 056 crypto Up/Down windows (9-16 Nov; 5m: last 240 s, 15m: last 600 s, 1h: last 1200 s) valued at
resolution: BUY earns (won) - price - fee, SELL earns price - (won) - fee per share. Per wallet: volume, edge, days in plus,
and style (time before the end, price, favourite or not, Binance move just before the trade in the bought direction, z lead)."""
import json, collections, time, math, numpy as np
import zmod
from zmod import PX, T0, sigma, tau, strike
W = {w["slug"]: w for w in json.load(open("windows.json"))}
MT = json.load(open("mtrades.json"))
def fee(p): return 0.07 * p * (1 - p)
LP = {a: np.log(p) for a, p in PX.items()}
agg = collections.defaultdict(lambda: collections.defaultdict(float))
days = collections.defaultdict(lambda: collections.defaultdict(float))
rows_out = []          # a sample of trades with features for the top wallets later
K_cache = {}
for slug, rows in MT.items():
    w = W.get(slug)
    if not w or not rows: continue
    a = w["asset"]; end = w["end"]; kind = w["kind"]
    if slug not in K_cache: K_cache[slug] = strike(w)
    K = K_cache[slug]
    lp = LP[a]
    for t, sd, o, sz, px, wal in rows:
        if sz <= 0 or not 0 < px < 1: continue
        won = (o == w["win"])
        e = ((1.0 if won else 0.0) - px - fee(px)) if sd == "B" else (px - (1.0 if won else 0.0) - fee(px))
        usd = sz * px
        x = end - t
        i = int(t) - T0
        # Binance move in the bought direction (Up bought or Down sold = +1)
        dirn = (1 if o == "Up" else -1) * (1 if sd == "B" else -1)
        if 5 <= i < len(lp):
            m2 = dirn * (lp[i] - lp[i - 2]) * 1e4
            m5 = dirn * (lp[i] - lp[i - 5]) * 1e4
            lead = dirn * (lp[i] - math.log(K)) * 1e4 if K else 0.0
        else:
            m2 = m5 = lead = 0.0
        g = agg[wal]
        g["n"] += 1; g["usd"] += usd; g["edge"] += sz * e; g["sh"] += sz
        g["buy"] += (sd == "B"); g["fav"] += (sd == "B" and px >= 0.5) or (sd == "S" and px < 0.5)
        g["x_" + ("after" if x < 0 else "last60" if x < 60 else "1-3m" if x < 180 else "3m+")] += sz * px
        g["p_" + ("<0.2" if px < 0.2 else "0.2-0.8" if px < 0.8 else "0.8-0.95" if px < 0.95 else ">=0.95")] += sz * px
        g["m2pos"] += (m2 > 0.5); g["m2neg"] += (m2 < -0.5)
        g["lead_with"] += (lead > 0)
        g["k_" + kind] += sz * px
        days[wal][time.strftime("%m-%d", time.gmtime(t))] += sz * e
out = []
for wal, g in agg.items():
    if g["n"] < 30: continue
    d = days[wal]
    out.append(dict(w=wal, n=int(g["n"]), usd=round(g["usd"], 2), edge=round(g["edge"], 2), roi=g["edge"] / g["usd"] if g["usd"] else 0,
                    days=len(d), dpos=sum(1 for v in d.values() if v > 0), buy=g["buy"] / g["n"], fav=g["fav"] / g["n"],
                    m2pos=g["m2pos"] / g["n"], m2neg=g["m2neg"] / g["n"], lead_with=g["lead_with"] / g["n"],
                    x={k[2:]: round(v / g["usd"], 3) for k, v in g.items() if k.startswith("x_")},
                    p={k[2:]: round(v / g["usd"], 3) for k, v in g.items() if k.startswith("p_")},
                    k={k[2:]: round(v / g["usd"], 3) for k, v in g.items() if k.startswith("k_")},
                    byday={k: round(v, 2) for k, v in sorted(d.items())}))
json.dump(out, open("wallets.json", "w"))
print("wallets with >= 30 taker trades:", len(out), " all taker trades:", sum(int(g["n"]) for g in agg.values()),
      " total taker edge $%.0f on $%.0f" % (sum(g["edge"] for g in agg.values()), sum(g["usd"] for g in agg.values())))
