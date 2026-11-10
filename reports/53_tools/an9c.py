"""Capacity: the same rules with larger orders (the VWAP of the taker prints of [s+1, s+2] must stay within the tier limit).
usage: an9c.py C9_*.json"""
import collections, json, sys
fee = lambda p: 0.07 * p * (1 - p)
C, nwin = [], {}
for f in sys.argv[1:]:
    d = json.load(open(f)); C += d["cands"]; nwin[d["asset"]] = d["windows"]
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def lim_for(p):
    best = None
    for pm, am in TIERS:
        if p >= pm and (best is None or am > best): best = am
    return best
def ok(c, floor, n, age):
    px = c["px"].get(str(n)); lim = lim_for(c["p"])
    if px is None or lim is None or not (floor <= px <= lim): return False
    for k in range(age):
        pp = c["prev"][k]
        if pp is None: return False
        l2 = lim_for(pp)
        if l2 is None or px > l2: return False
    return True
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
def entries(floor, n, age, assets=None):
    out = []
    for (a, _), v in by.items():
        if assets and a not in assets: continue
        for c in v:
            if ok(c, floor, n, age): out.append(c); break
    return out
def pnl(c, n):
    px = c["px"][str(n)]
    return n * ((1 - px) if c["won"] else -px) - n * fee(px)
DAYS = 8.8
print("signal at least 1 s old; P&L without risk limits; 8.8 days")
for name, floor in (("L3 (px >= 0.90)", 0.90), ("L4 (px >= 0.50)", 0.50)):
    print(f"\n=== {name}")
    print(f"{'shares':>7s} {'coins':>16s} {'entries/day':>11s} {'lost %':>7s} {'c/share':>8s} {'$ per day':>10s} {'$ at risk per entry':>20s}")
    for assets, label in ((None, "all four"), (("btc",), "btc"), (("eth",), "eth"), (("sol", "xrp"), "sol+xrp")):
        for n in (5, 10, 20, 50, 100, 200):
            e = entries(floor, n, 1, assets)
            if not e: 
                print(f"{n:7d} {label:>16s} {'0':>11s}"); continue
            tot = sum(pnl(c, n) for c in e); lost = sum(not c["won"] for c in e)
            apx = sum(c["px"][str(n)] for c in e) / len(e)
            print(f"{n:7d} {label:>16s} {len(e)/DAYS:11.1f} {100*lost/len(e):7.1f} {100*tot/(n*len(e)):+8.2f} {tot/DAYS:+10.2f} {n*apx:20.0f}")
# how much volume do takers buy on the decided side within the limit, per signal second
print("\ntaker volume bought on the model's side in the 2 s after a valid L3 signal (shares), by coin: median / p75 / p90")
for a in nwin:
    v = sorted(c["vol"] for c in C if c["a"] == a and ok(c, 0.90, 5, 0))
    if v: print(f"   {a}: {v[len(v)//2]:.0f} / {v[int(.75*len(v))]:.0f} / {v[int(.9*len(v))]:.0f}   (signal-seconds: {len(v)})")
