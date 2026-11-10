"""Fixed vs volatility-scaled shock pause on the candidate lists of reports/53_tools/cands9.py built with the extra field
"sh" = largest |3 s Binance move| (bp, 1 s closes) that ended in the last 10 s:
    sh=round(max(abs(math.log(P(now - k) / P(now - k - 3))) * 1e4 for k in range(0, 11)), 3)
usage: shock_scan.py S9_btc.json S9_eth.json S9_sol.json S9_xrp.json"""
import collections, json, math, sys
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
def ok(c, floor=0.90, age=1):
    px = c["px"].get("5"); lim = lim_for(c["p"])
    if px is None or lim is None or not (floor <= px <= lim): return False
    for k in range(age):
        pp = c["prev"][k]
        if pp is None or lim_for(pp) is None or px > lim_for(pp): return False
    return True
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
def run(flt, floor=0.90):
    out = []
    for v in by.values():
        for c in v:
            if flt(c) and ok(c, floor): out.append(c); break
    return out
def pnl(c):
    px = c["px"]["5"]; return 5 * ((1 - px) if c["won"] else -px) - 5 * fee(px)
def s(v):
    if not v: return "0"
    tot = sum(pnl(c) for c in v); lost = sum(not c["won"] for c in v)
    return f"{len(v):4d} entries ({len(v)/8.8:5.1f}/day)  lost {lost:2d} ({100*lost/len(v):4.1f}%)  {100*tot/(5*len(v)):+5.2f}c/sh  {tot/8.8:+6.2f}$/day"
for name, floor in (("L3 (px >= 0.90)", 0.90), ("L4 (px >= 0.50)", 0.50)):
    print(f"\n=== {name}, signal >= 1 s old, 1-9 Nov (Binance 1 s closes)")
    print("  no pause:                         ", s(run(lambda c: True, floor)))
    for thr in (4.0, 3.0, 2.5, 2.0):
        print(f"  pause if 3 s move > {thr} bp:        ", s(run(lambda c: c["sh"] <= thr, floor)))
    for k in (2.5, 3.0):
        print(f"  pause if > max(2.5 bp, {k} sigma*sqrt3):", s(run(lambda c: c["sh"] <= max(2.5, k * c["sig"] * math.sqrt(3)), floor)))
    e = run(lambda c: True, floor)
    print("  entries of the plain rule by volatility regime (sigma bp):")
    for lo, hi_ in ((0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99)):
        v = [c for c in e if lo <= c["sig"] < hi_]
        blocked = [c for c in v if c["sh"] > 2.5]
        print(f"     sigma [{lo},{hi_}): {s(v)} | of them the 2.5 bp pause would block {len(blocked)} ({sum(not c['won'] for c in blocked)} losers)")
