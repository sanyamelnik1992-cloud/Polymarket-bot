"""How many wins one loss costs, by entry price, and what the L3 rule of reports/59 (bottom tier from second 260, live pause)
gives at larger sizes. Candidate lists of reports/53_tools/cands9.py with the "sh" field (reports/58_tools/shock_scan.py).
usage: ratio.py S9_btc.json S9_eth.json S9_sol.json S9_xrp.json"""
import collections, json, math, sys
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[1:]:
    C += json.load(open(f))["cands"]
TOP = ((.99, .98), (.97, .95), (.95, .93)); LOW = (.90, .91)
def lim_for(p, low_ok):
    best = None
    for pm, am in TOP + ((LOW,) if low_ok else ()):
        if p >= pm and (best is None or am > best): best = am
    return best
def ok(c, n="5", low_from=260, floor=0.90):
    low_ok = c["s"] >= low_from
    px = c["px"].get(n); lim = lim_for(c["p"], low_ok)
    if px is None or lim is None or not (floor <= px <= lim): return False
    pp = c["prev"][0]
    return pp is not None and lim_for(pp, low_ok) is not None and px <= lim_for(pp, low_ok)
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
pause = lambda c: c["sh"] <= max(2.5, 3.0 * c["sig"] * math.sqrt(3))
def run(n="5", flt=lambda c: True, **kw):
    out = []
    for v in by.values():
        for c in v:
            if pause(c) and flt(c) and ok(c, n, **kw): out.append(c); break
    return out
DAYS = 8.8
def line(v, n="5"):
    if not v: return "   0"
    k = float(n); pn = [k * ((1 - c["px"][n]) if c["won"] else -c["px"][n]) - k * fee(c["px"][n]) for c in v]
    lost = sum(not c["won"] for c in v); tot = sum(pn)
    apx = sum(c["px"][n] for c in v) / len(v)
    be = apx + fee(apx)                                   # win rate needed for zero
    wins_per_loss = be / (1 - be)
    byday = collections.defaultdict(float)
    for c, x in zip(v, pn): byday[c["ws"] // 86400] += x
    return (f"{len(v):4d} entries  lost {lost:2d} ({100*lost/len(v):4.1f}%)  avg price {apx:.3f}  zero at {100*(1-be):4.1f}% lost  "
            f"1 loss = {wins_per_loss:5.1f} wins  {100*tot/(k*len(v)):+5.2f}c/sh  {tot/DAYS:+7.2f}$/day  worst day {min(byday.values()):+7.1f}$")
e = run()
print("=== L3 as in reports/59 (bottom tier from second 260), 5 shares, 1-9 Nov: by entry price")
for lo, hi in ((0.90, 0.915), (0.915, 0.935), (0.935, 0.955), (0.955, 0.985)):
    print(f"  price [{lo},{hi}): ", line([c for c in e if lo <= c["px"]["5"] < hi]))
print("  all:                 ", line(e))
print("\n=== by the model's probability at entry (calibration of the entries)")
for lo, hi in ((0.90, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 0.999), (0.999, 1.01)):
    print(f"  p [{lo},{hi}): ", line([c for c in e if lo <= c["p"] < hi]))
print("\n=== by window second of the entry")
for lo, hi in ((240, 250), (250, 260), (260, 270), (270, 280), (280, 300)):
    print(f"  second [{lo},{hi}): ", line([c for c in e if lo <= c["s"] < hi]))
print("\n=== the same rule by order size (upper bound: the volume other takers got in the next 2 s)")
for n in ("5", "10", "20", "50", "100", "200"):
    v = run(n)
    print(f"  {n:>3s} shares: ", line(v, n), f" at risk per entry ${float(n) * sum(c['px'][n] for c in v) / max(len(v), 1):.0f}")
    if n in ("50", "200"):
        for a in ("btc", "eth", "sol", "xrp"):
            print(f"       {a}: ", line([c for c in v if c["a"] == a], n))
print("\n=== stricter variants, 5 shares")
print("  p >= 0.99 only (rule L):          ", line(run(flt=lambda c: c["p"] >= 0.99)))
print("  p >= 0.97 tiers only:             ", line(run(flt=lambda c: c["p"] >= 0.97)))
print("  every tier from second 260:       ", line(run(flt=lambda c: c["s"] >= 260)))
print("  every tier from second 270:       ", line(run(flt=lambda c: c["s"] >= 270)))
print("  price floor 0.80 (L4 zone), from 260 below p 0.95:", line(run(floor=0.80)))
print("  price floor 0.50, from 260 below p 0.95:", line(run(floor=0.50)))
