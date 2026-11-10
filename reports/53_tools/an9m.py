"""Maker side of the lock: when the model's side was sold by takers in the closing minute, what did the makers who bought it get?
First such print per window and coin at a price within the rule; maker pays no fee and gets 20% of the taker fee back.
usage: an9m.py M9_*.json"""
import collections, json, sys
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[1:]:
    C += json.load(open(f))["cands"]
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
def run(rule, age=1):
    out = []
    for v in by.values():
        for c in v:
            px = c["px"].get("5")
            if px is None or not rule(c["p"], px): continue
            if any(c["prev"][k] is None or not rule(c["prev"][k], px) for k in range(age)): continue
            out.append(c); break
    return out
def pnl(c, improve=0.0):
    px = c["px"]["5"] + improve
    return 5 * ((1 - px) if c["won"] else -px) + 5 * 0.2 * fee(px)
def s(v, improve=0.0):
    if not v: return "   0"
    tot = sum(pnl(c, improve) for c in v); lost = sum(not c["won"] for c in v)
    return f"{len(v):4d} ({len(v)/8.8:5.1f}/day) lost {lost:3d} ({100*lost/len(v):4.1f}%) avg px {sum(c['px']['5'] for c in v)/len(v):.3f} {100*tot/(5*len(v)):+5.2f}c/sh {tot/8.8:+6.2f}$/day"
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def tier(floor):
    return lambda p, px: floor <= px and any(p >= pm and px <= am for pm, am in TIERS)
print("maker fills on the model's side (signal >= 1 s old), 5 shares, first per window:")
for name, rule in (("LM  rule: p >= .99, price 0.90-0.98", lambda p, px: p >= 0.99 and 0.90 <= px <= 0.98),
                   ("LM2 rule: p >= .99, price 0.90-0.96", lambda p, px: p >= 0.99 and 0.90 <= px <= 0.96),
                   ("tiers, price >= 0.90 (L3 as a maker)", tier(0.90)), ("tiers, price >= 0.50 (L4 as a maker)", tier(0.50))):
    e = run(rule)
    print(f"\n {name}\n   at the makers' own price:        {s(e)}")
    print(f"   paying 1c more to be first:      {s(e, 0.01)}")
    for a in ("btc", "eth", "sol", "xrp"):
        print(f"      {a}: {s([c for c in e if c['a'] == a])}")
