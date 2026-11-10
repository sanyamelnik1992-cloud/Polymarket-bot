"""Which lock entries can a slow taker really get? Split the L3 / L4 entries of the 9-day tapes by how long the signal had been
there and by what Binance did in the seconds before; then the same rules restricted to signals that are at least N s old.
usage: an9b.py C9_*.json"""
import collections, json, sys, time
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
def ok(c, floor, n=5, age=0):
    px = c["px"].get(str(n)); lim = lim_for(c["p"])
    if px is None or lim is None or not (floor <= px <= lim): return False
    # the signal must have been there `age` seconds ago as well: the earlier model probability already allowed this price
    for k in range(age):
        pp = c["prev"][k]
        if pp is None: return False
        l2 = lim_for(pp)
        if l2 is None or px > l2: return False
    return True
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
def entries(floor, n=5, age=0, flt=lambda c: True):
    out = []
    for v in by.values():
        for c in v:
            if flt(c) and ok(c, floor, n, age): out.append(c); break
    return out
def pnl(c, n=5):
    px = c["px"][str(n)]
    return n * ((1 - px) if c["won"] else -px) - n * fee(px)
def stat(ent, n=5):
    if not ent: return "   0 entries"
    tot = sum(pnl(c, n) for c in ent); lost = sum(not c["won"] for c in ent)
    days = len({c["ws"] // 86400 for c in ent})
    return (f"{len(ent):4d} entries ({len(ent)/8.8:5.1f}/day), {lost:3d} lost ({100*lost/len(ent):4.1f}%), P&L {tot:+8.2f}$ ({tot/8.8:+6.2f}/day), "
            f"{100*tot/(n*len(ent)):+5.2f}c/sh, avg px {sum(c['px'][str(n)] for c in ent)/len(ent):.3f}")
for name, floor in (("L3 (px >= 0.90)", 0.90), ("L4 (px >= 0.50)", 0.50)):
    ent = entries(floor)
    print(f"\n=== {name}: all first signals: {stat(ent)}")
    fresh = [c for c in ent if not ok(c, floor, 5, 1)]
    old1 = [c for c in ent if ok(c, floor, 5, 1)]
    print(f"   signal is NEW this second (was not there 1 s ago): {stat(fresh)}")
    print(f"   signal already there 1 s ago:                     {stat(old1)}")
    print("   by Binance move over the last 3 s in our favour (bp):")
    for lo, hi in ((-2.1, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 3.0), (3.0, 99)):
        v = [c for c in ent if lo <= c["mv3"] < hi]
        print(f"      mv3 [{lo:+.1f},{hi:+.1f}): {stat(v)}")
    for age in (1, 2, 3):
        e2 = entries(floor, 5, age)
        print(f"   RULE with a signal at least {age} s old: {stat(e2)}")
        if age == 2:
            for a in nwin: print(f"        {a}: {stat([c for c in e2 if c['a'] == a])}")
            dd = collections.defaultdict(list)
            for c in e2: dd[time.strftime('%d', time.gmtime(c['ws']))].append(c)
            print("        by day: " + " | ".join(f"{d}: {len(v)}/{sum(not c['won'] for c in v)} {sum(pnl(c) for c in v):+.1f}" for d, v in sorted(dd.items())))
            for lo, hi in ((0.50, 0.80), (0.80, 0.90), (0.90, 0.915), (0.915, 0.985)):
                v = [c for c in e2 if lo <= c["px"]["5"] < hi]
                if v: print(f"        px [{lo:.3f},{hi:.3f}): {stat(v)}")
