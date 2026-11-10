"""Start-second test: the same rules with decisions allowed only from second REL_LO (240 = live). Two halves of the period.
usage: an9e.py C9_*.json"""
import collections, json, sys
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[1:]:
    C += json.load(open(f))["cands"]
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def lim_for(p, tiers=TIERS):
    best = None
    for pm, am in tiers:
        if p >= pm and (best is None or am > best): best = am
    return best
def ok(c, floor, age, tiers=TIERS):
    px = c["px"].get("5"); lim = lim_for(c["p"], tiers)
    if px is None or lim is None or not (floor <= px <= lim): return False
    for k in range(age):
        pp = c["prev"][k]
        if pp is None: return False
        l2 = lim_for(pp, tiers)
        if l2 is None or px > l2: return False
    return True
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
D0 = 1794182400 - 8 * 86400
def run(floor, rel_lo, age=1, tiers=TIERS, flt=lambda c: True):
    out = []
    for v in by.values():
        for c in v:
            if c["s"] >= rel_lo and flt(c) and ok(c, floor, age, tiers): out.append(c); break
    return out
def pnl(c):
    px = c["px"]["5"]
    return 5 * ((1 - px) if c["won"] else -px) - 5 * fee(px)
def s(v):
    if not v: return "    0"
    tot = sum(pnl(c) for c in v); lost = sum(not c["won"] for c in v)
    return f"{len(v):4d} / {lost:2d} lost ({100*lost/len(v):4.1f}%) {tot:+7.1f}$ {100*tot/(5*len(v)):+5.2f}c"
h1 = lambda c: c["ws"] < D0 + 4.4 * 86400
for name, floor in (("L3 (px >= 0.90)", 0.90), ("L4 (px >= 0.50)", 0.50)):
    print(f"\n=== {name}, signal >= 1 s old:  whole period | 1 Nov - 5 Nov 09:36 | 5 Nov 09:36 - 9 Nov")
    for rel_lo in (240, 241, 242, 243, 244, 245, 247, 250, 255):
        e = run(floor, rel_lo)
        print(f"   from second {rel_lo}:  {s(e)} | {s([c for c in e if h1(c)])} | {s([c for c in e if not h1(c)])}")
print("\n=== where do the entries of the first seconds go? L3, rel_lo 240 vs 245: the windows entered at 240-244 under the live rule")
e240 = {(c["a"], c["ws"]): c for c in run(0.90, 240)}
e245 = {(c["a"], c["ws"]): c for c in run(0.90, 245)}
early = {k: c for k, c in e240.items() if c["s"] < 245}
still = [e245[k] for k in early if k in e245]
gone = [c for k, c in early.items() if k not in e245]
print("   entered at 240-244 under the live rule:", s(list(early.values())))
print("   ... of them still entered later with rel_lo 245:", s(still), "(the later entry)")
print("   ... of them not entered at all with rel_lo 245: ", s(gone), "(what the live rule got there)")
new = [c for k, c in e245.items() if k not in e240]
print("   new windows that only rel_lo 245 enters:", s(new))
