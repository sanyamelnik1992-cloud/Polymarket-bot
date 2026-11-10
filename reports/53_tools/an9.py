"""Rules on the 9-day candidate lists (cands9.py): entries, losses, P&L by rule / period / coin / tier / size.
usage: an9.py C9_btc.json C9_eth.json ..."""
import collections, json, sys, time
fee = lambda p: 0.07 * p * (1 - p)
C, nwin = [], {}
for f in sys.argv[1:]:
    d = json.load(open(f)); C += d["cands"]; nwin[d["asset"]] = d["windows"]
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def tier_lim(p):
    best = None
    for pm, am in TIERS:
        if p >= pm and (best is None or am > best): best = am
    return best
def rule_L(c, n=5):
    px = c["px"].get(str(n)) if isinstance(next(iter(c["px"])), str) else c["px"].get(n)
    return px is not None and c["p"] >= 0.99 and 0.90 <= px <= min(0.98, c["p"] - 0.005 - fee(px))
def mk(floor):
    def r(c, n=5):
        px = c["px"].get(str(n))
        lim = tier_lim(c["p"])
        return px is not None and lim is not None and floor <= px <= lim
    return r
RULES = {"L": rule_L, "L3": mk(0.90), "L4": mk(0.50)}
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
def entries(rule, n=5):
    out = []
    for v in by.values():
        for c in v:
            if rule(c, n): out.append(c); break
    return out
def pnl(c, n=5):
    px = c["px"][str(n)]
    return n * ((1 - px) if c["won"] else -px) - n * fee(px)
def stat(ent, n=5):
    if not ent: return "0 entries"
    tot = sum(pnl(c, n) for c in ent); lost = sum(not c["won"] for c in ent)
    apx = sum(c["px"][str(n)] for c in ent) / len(ent)
    return f"{len(ent):4d} entries, {lost:3d} lost ({100*lost/len(ent):4.1f}%), P&L {tot:+8.2f}$, {100*tot/(n*len(ent)):+5.2f}c/sh, avg px {apx:.3f}"
def live_sim(ent, cap=5.0, stop=5.0, n=5):
    """One entry at a time (open cost <= cap until the window ends), day stop at -stop realized."""
    ev = sorted(ent, key=lambda c: (c["ws"] + c["s"], -(c["p"] - c["px"][str(n)])))
    days = collections.defaultdict(lambda: {"n": 0, "lost": 0, "pnl": 0.0, "stopped": False})
    open_until, realized, pending = 0, collections.defaultdict(float), []
    open_cost = 0.0; opens = []
    for c in ev:
        t = c["ws"] + c["s"]; day = c["ws"] // 86400
        pending.sort()
        while pending and pending[0][0] <= t:
            _, d, pn = pending.pop(0); realized[d] += pn
        opens = [(e, k) for e, k in opens if e > t]
        if days[day]["stopped"] or realized[day] <= -stop:
            days[day]["stopped"] = True; continue
        cost = n * c["px"][str(n)] + n * fee(c["px"][str(n)])
        if sum(k for _, k in opens) + n * tier_or(c) > cap * n / 5 + 1e-9: continue
        opens.append((c["ws"] + 302, cost)); pending.append((c["ws"] + 302, day, pnl(c, n)))
        d = days[day]; d["n"] += 1; d["lost"] += not c["won"]; d["pnl"] += pnl(c, n)
    return days
def tier_or(c):
    return tier_lim(c["p"]) or 0.98
tot = sum(nwin.values())
D0 = 1794182400 - 8 * 86400
dname = lambda ws: time.strftime("%d", time.gmtime(ws))
print(f"windows {tot}: " + ", ".join(f"{a} {n}" for a, n in nwin.items()), "| 1 Nov 00:00 - 9 Nov 19:15 UTC")
for name, rule in RULES.items():
    ent = entries(rule)
    print(f"\n=== {name} (no limits, 5 shares): {stat(ent)}")
    for label, f in (("1-4 Nov (before any tuning data)", lambda c: c["ws"] < D0 + 4 * 86400), ("5-9 Nov", lambda c: c["ws"] >= D0 + 4 * 86400)):
        print(f"   {label:34s} {stat([c for c in ent if f(c)])}")
    for a in nwin:
        print(f"   {a:34s} {stat([c for c in ent if c['a'] == a])}")
    dd = collections.defaultdict(list)
    for c in ent: dd[dname(c["ws"])].append(c)
    print("   by day: " + " | ".join(f"{d}: {len(v)}/{sum(not c['won'] for c in v)} {sum(pnl(c) for c in v):+.1f}" for d, v in sorted(dd.items())))
    sim = live_sim(ent)
    n = sum(x["n"] for x in sim.values()); lost = sum(x["lost"] for x in sim.values()); p = sum(x["pnl"] for x in sim.values())
    print(f"   live limits ($5 open = one at a time, stop -$5/day): {n} entries, {lost} lost, P&L {p:+.2f}$, per day {p/len(sim):+.2f}$, "
          f"worst day {min(x['pnl'] for x in sim.values()):+.2f}$, days stopped {sum(x['stopped'] for x in sim.values())} of {len(sim)}")
    print("   by day with limits: " + " | ".join(f"{time.strftime('%d', time.gmtime(d*86400))}: {x['n']}/{x['lost']} {x['pnl']:+.1f}{'*' if x['stopped'] else ''}" for d, x in sorted(sim.items())))
ent = entries(RULES["L4"])
print("\n=== L4 entries by price band and model p (5 shares)")
for lo, hi in ((0.50, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.915), (0.915, 0.935), (0.935, 0.955), (0.955, 0.985)):
    v = [c for c in ent if lo <= c["px"]["5"] < hi]
    print(f"   px [{lo:.3f},{hi:.3f}): {stat(v)}")
for lo, hi in ((0.90, 0.92), (0.92, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 1.01)):
    v = [c for c in ent if lo <= c["p"] < hi]
    print(f"   p  [{lo:.2f},{hi:.2f}): {stat(v)}")
