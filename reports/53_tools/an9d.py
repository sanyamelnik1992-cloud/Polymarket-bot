"""What separates the losing lock entries? Loss rate and cents per share by feature bucket, L4 entries (signal >= 1 s old).
usage: an9d.py C9_*.json"""
import collections, json, math, sys, time
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[1:]:
    C += json.load(open(f))["cands"]
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def lim_for(p):
    best = None
    for pm, am in TIERS:
        if p >= pm and (best is None or am > best): best = am
    return best
def ok(c, floor, n=5, age=1):
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
ent = []
for v in by.values():
    for c in v:
        if ok(c, 0.50): ent.append(c); break
def pnl(c):
    px = c["px"]["5"]
    return 5 * ((1 - px) if c["won"] else -px) - 5 * fee(px)
def line(label, v):
    if len(v) < 8: return
    tot = sum(pnl(c) for c in v); lost = sum(not c["won"] for c in v)
    apx = sum(c["px"]["5"] for c in v) / len(v)
    print(f"   {label:26s} n {len(v):4d}  lost {100*lost/len(v):5.1f}%  avg px {apx:.3f}  implied loss {100*(1-apx):5.1f}%  {100*tot/(5*len(v)):+6.2f}c/sh  P&L {tot:+7.1f}$")
def table(title, key, edges, sub=ent):
    print(f"\n{title}")
    for lo, hi in zip(edges[:-1], edges[1:]):
        line(f"[{lo:g}, {hi:g})", [c for c in sub if lo <= key(c) < hi])
print(f"L4 entries, signal >= 1 s old: {len(ent)}, lost {sum(not c['won'] for c in ent)}")
line("ALL", ent)
table("second of the window", lambda c: c["s"], [240, 243, 248, 255, 265, 280, 297])
table("prints still unknown (m)", lambda c: c["m"], [0, 15, 30, 40, 50, 56, 61])
table("model p", lambda c: c["p"], [0.90, 0.92, 0.95, 0.97, 0.99, 1.01])
table("model p minus price paid", lambda c: c["p"] - c["px"]["5"], [-0.1, 0.0, 0.02, 0.05, 0.10, 0.20, 0.6])
table("distance of the expected TWAP from the strike, bp", lambda c: c["dist"], [0.5, 1, 2, 3, 5, 8, 100])
table("sigma (EWMA 900 s), bp per sqrt(s)", lambda c: c["sig"], [0, 0.12, 0.18, 0.25, 0.35, 0.5, 5])
table("vol burst: rms of last 30 s / sigma", lambda c: c["v30"] / c["sig"] if c["sig"] else 0, [0, 0.6, 0.9, 1.2, 1.6, 2.2, 50])
table("Binance last 10 s in our favour, bp", lambda c: c["mv10"], [-50, -2, -0.5, 0.5, 2, 4, 50])
table("Binance last 30 s in our favour, bp", lambda c: c["mv30"], [-50, -3, -1, 1, 3, 6, 50])
table("hour UTC", lambda c: time.gmtime(c["ws"]).tm_hour, [0, 4, 8, 12, 16, 20, 24])
print("\ncoin")
for a in ("btc", "eth", "sol", "xrp"): line(a, [c for c in ent if c["a"] == a])
print("\nprice paid vs the last taker price of our side in the 2 s before (market moving toward / away from us)")
for lab, f in (("no print before", lambda c: c["pxb"] is None), ("price rose >= 2c", lambda c: c["pxb"] is not None and c["px"]["5"] - c["pxb"] >= 0.02),
               ("about the same", lambda c: c["pxb"] is not None and abs(c["px"]["5"] - c["pxb"]) < 0.02), ("price fell >= 2c", lambda c: c["pxb"] is not None and c["px"]["5"] - c["pxb"] <= -0.02)):
    line(lab, [c for c in ent if f(c)])
