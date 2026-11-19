"""16 days (1-16 Nov) of the early rule on trades: combined 5m + 15m, by day / coin, drawdown, worst minute (several coins lost together)."""
import calendar, collections, time
import numpy as np
from sim_all import run, fee
D = lambda d: calendar.timegm((2026, 11, d, 0, 0, 0))
pn = lambda e: (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
for name, rules in (("brother: 5m z>=3 x120-62 + 15m z>=3 x300-120", [("5m", 3.0, 120, 62), ("15m", 3.0, 300, 120)]),
                    ("5m z>=3 x180-62 + 15m z>=3 x300-120", [("5m", 3.0, 180, 62), ("15m", 3.0, 300, 120)]),
                    ("5m z>=3.5 x180-62 + 15m z>=3.5 x300-120", [("5m", 3.5, 180, 62), ("15m", 3.5, 300, 120)])):
    ent = []
    for kind, z0, xh, xl in rules:
        ent += [dict(e, kind=kind) for e in run(kind, z0, xh, xl, 0.99, 0.985, D(1), D(17))]
    ent.sort(key=lambda e: e["t"])
    n = len(ent); lost = sum(not e["won"] for e in ent); tot = sum(pn(e) for e in ent)
    by = collections.defaultdict(float)
    for e in ent: by[time.strftime("%m-%d", time.gmtime(e["t"]))] += pn(e)
    days = sorted(by.items())
    for sh in (5, 10):
        eq = mx = dd = 0.0
        for e in ent: eq += sh * pn(e); mx = max(mx, eq); dd = max(dd, mx - eq)
        print(f"{name} | {sh} sh: {n} entries ({n/16:.0f}/day), lost {lost} ({100*lost/n:.2f}%), total ${sh*tot:+.1f} (${sh*tot/16:+.2f}/day), "
              f"negative days {sum(v < 0 for _, v in days)}/16, worst day ${sh*min(v for _, v in days):+.1f}, max drawdown ${dd:.1f}")
    lm = collections.Counter(e["t"] // 60 for e in ent if not e["won"])
    print("   losses in the same minute:", collections.Counter(lm.values()), "| by coin:",
          {a: (sum(1 for e in ent if e["asset"] == a), sum(1 for e in ent if e["asset"] == a and not e["won"]), round(10 * sum(pn(e) for e in ent if e["asset"] == a), 1)) for a in ("btc", "eth", "sol", "xrp")})
