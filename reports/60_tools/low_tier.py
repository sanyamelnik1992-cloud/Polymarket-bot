"""The bottom tier of L3 (p 0.90-0.95 at an ask <= 0.91) by entry second, on the candidate lists of reports/53_tools/cands9.py
(built with the "sh" field, see reports/58_tools/shock_scan.py). A candidate's "s" is the window second, "m" = 298 - s the
closing-TWAP prints still unknown. Variants: the bottom tier only from second X on, only above a probability, not at all.
usage: low_tier.py S9_btc.json S9_eth.json S9_sol.json S9_xrp.json"""
import collections, json, math, sys
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[1:]:
    C += json.load(open(f))["cands"]
TOP = ((.99, .98), (.97, .95), (.95, .93))
LOW = (.90, .91)
SPLIT = min(c["ws"] for c in C) + (max(c["ws"] for c in C) - min(c["ws"] for c in C)) // 2
def lim_for(p, low_ok=True, low_p=LOW[0]):
    best = None
    for pm, am in TOP + (((low_p, LOW[1]),) if low_ok else ()):
        if p >= pm and (best is None or am > best): best = am
    return best
def ok(c, low_from=0, low_p=LOW[0], low_coins=None, age=1):
    low_ok = c["s"] >= low_from and (low_coins is None or c["a"] in low_coins)
    px = c["px"].get("5"); lim = lim_for(c["p"], low_ok, low_p)
    if px is None or lim is None or not (0.90 <= px <= lim): return False
    for k in range(age):
        pp = c["prev"][k]
        if pp is None or lim_for(pp, low_ok, low_p) is None or px > lim_for(pp, low_ok, low_p): return False
    return True
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
pause = lambda c: c["sh"] <= max(2.5, 3.0 * c["sig"] * math.sqrt(3))      # the live pause since reports/58
def run(flt=pause, **kw):
    out = []
    for v in by.values():
        for c in v:
            if flt(c) and ok(c, **kw): out.append(c); break
    return out
def pnl(c):
    px = c["px"]["5"]; return 5 * ((1 - px) if c["won"] else -px) - 5 * fee(px)
def s(v):
    if not v: return "0"
    tot = sum(pnl(c) for c in v); lost = sum(not c["won"] for c in v)
    h1 = sum(pnl(c) for c in v if c["ws"] < SPLIT); h2 = tot - h1
    return (f"{len(v):4d} entries ({len(v)/8.8:5.1f}/day)  lost {lost:2d} ({100*lost/len(v):4.1f}%)  {100*tot/(5*len(v)):+5.2f}c/sh  "
            f"{tot/8.8:+6.2f}$/day  halves {h1:+6.1f} / {h2:+6.1f}")
print("=== L3 with the live pause max(2.5 bp, 3 sigma sqrt3), signal >= 1 s old, 5 shares, 1-9 Nov")
base = run()
print("  as live now:                      ", s(base))
print("  no bottom tier:                   ", s(run(low_from=999)))
for x in (250, 255, 260, 263, 266, 270, 275):
    print(f"  bottom tier from second {x}:      ", s(run(low_from=x)))
for p in (0.92, 0.93):
    print(f"  bottom tier needs p >= {p}:      ", s(run(low_p=p)))
print("  no bottom tier on btc:            ", s(run(low_coins=("eth", "sol", "xrp"))))
print("  bottom tier from 263, not on btc: ", s(run(low_from=263, low_coins=("eth", "sol", "xrp"))))
low = [c for c in base if c["p"] < 0.95]
print("\n=== entries of the bottom tier (p < 0.95) of the rule as live now, by entry second")
for lo, hi in ((240, 250), (250, 263), (263, 275), (275, 300)):
    print(f"  second [{lo},{hi}): ", s([c for c in low if lo <= c["s"] < hi]))
print("  by coin:")
for a in ("btc", "eth", "sol", "xrp"):
    print(f"  {a}:              ", s([c for c in low if c["a"] == a]))
print("  the other tiers (p >= 0.95):      ", s([c for c in base if c["p"] >= 0.95]))
print("\n=== by day: as live now | bottom tier from second 263")
alt = run(low_from=263)
days = sorted({c["ws"] // 86400 for c in base})
for d in days:
    a = [c for c in base if c["ws"] // 86400 == d]; b = [c for c in alt if c["ws"] // 86400 == d]
    print(f"  day {d - days[0] + 1}: {len(a):3d} entries, lost {sum(not c['won'] for c in a):2d}, {sum(map(pnl, a)):+6.2f}$  |  "
          f"{len(b):3d} entries, lost {sum(not c['won'] for c in b):2d}, {sum(map(pnl, b)):+6.2f}$")
