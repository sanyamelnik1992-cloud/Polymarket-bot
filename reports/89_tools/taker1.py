import json, sys, collections, time, math
import numpy as np
from fairlib import p_up, has, mom, fee, sigma
raw = json.load(open("who7_raw.json")); oc = json.load(open("outcomes.json"))
pref = sys.argv[1]
w = [k for k in raw if k.startswith(pref)][0]
T = sorted(raw[w]["taker"], key=lambda x: x["timestamp"])
rows = []
seq = collections.Counter()
for x in T:
    s = x["slug"]; a = s.split("-")[0]; dur = 300 if "-5m-" in s else 900
    start = int(s.rsplit("-", 1)[1]); t = int(x["timestamp"])
    if s not in oc or not has(a, t) or not has(a, start - 62 - 3600): continue
    side = x["outcome"]; p = float(x["price"]); n = float(x["size"])
    won = oc[s] == side
    seq[s] += 1
    r = dict(s=s, a=a, t=t, rel=t - start, side=side, p=p, n=n, usd=p * n, won=won, k=seq[s],
             pnl=n * ((1.0 if won else 0.0) - p) - n * fee(p) if x["side"] == "BUY" else None)
    for L in (0, 1, 2, 3, 5, 10):
        pu = p_up(a, start, dur, t, L)
        r[f"f{L}"] = None if pu is None else (pu if side == "Up" else 1 - pu)
    sg = 1 if side == "Up" else -1
    for k in (1, 3, 10, 30, 60):
        r[f"m{k}"] = sg * mom(a, t, k)
    r["sig"] = sigma(a, t) * 1e4
    rows.append(r)
json.dump(rows, open(f"t_{pref}.json", "w"))
tot = sum(r["pnl"] for r in rows); usd = sum(r["usd"] for r in rows)
print(f"trades {len(rows)} windows {len({r['s'] for r in rows})} usd {usd:.0f} pnl {tot:.0f} ({100 * tot / usd:.1f} %) won {sum(r['won'] for r in rows)}")
def tab(name, key, edges):
    print(f"\n{name}: trades / usd / won-share (usd-weighted) / paid avg / pnl / roi")
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = [r for r in rows if r[key] is not None and lo <= r[key] < hi]
        if not g: continue
        u = sum(r["usd"] for r in g); pn = sum(r["pnl"] for r in g)
        wsh = sum(r["usd"] * r["won"] for r in g) / u; pa = sum(r["usd"] * r["p"] for r in g) / u
        print(f"  {lo:>7} .. {hi:<7} {len(g):5d} {u:9.0f}  won {wsh:5.2f}  paid {pa:5.2f}  pnl {pn:8.0f}  {100 * pn / u:6.1f} %")
for r in rows:
    for L in (0, 1, 2, 3, 5, 10):
        r[f"e{L}"] = None if r[f"f{L}"] is None else r[f"f{L}"] - r["p"]
E = [-1, -0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.15, 0.2, 1]
for L in (0, 2, 5, 10):
    tab(f"edge vs Binance fair, Binance {L} s before", f"e{L}", E)
tab("fair value of his side (Binance 2 s before)", "f2", [0, .2, .4, .5, .6, .8, 1.01])
tab("price paid", "p", [0, .2, .4, .5, .6, .8, 1.01])
tab("second of the window", "rel", [0, 30, 60, 90, 120, 180, 240, 300])
tab("Binance move toward his side over the last 10 s, bp", "m10", [-99, -5, -2, -0.5, 0.5, 2, 5, 99])
tab("Binance move toward his side over the last 60 s, bp", "m60", [-99, -10, -5, -2, 0, 2, 5, 10, 99])
tab("n-th trade in the window", "k", [1, 2, 4, 8, 100])
byday = collections.defaultdict(lambda: [0, 0, 0])
for r in rows:
    d = time.strftime("%m-%d", time.gmtime(r["t"])); byday[d][0] += 1; byday[d][1] += r["usd"]; byday[d][2] += r["pnl"]
print("\nby day:", " ".join(f"{d} {v[0]}/{v[1]:.0f}/{v[2]:+.0f}" for d, v in sorted(byday.items())))
