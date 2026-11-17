"""Stability of a rule: by UTC day, asset, time left; concurrency (open entries at once)."""
import json, sys, time, collections
from sim import run, fee
for kind, args in (("5m", dict(z0=3, x_hi=180, x_lo=62, cap=0.99)), ("5m", dict(z0=4, x_hi=180, x_lo=62, cap=0.99)),
                   ("15m", dict(z0=3, x_hi=420, x_lo=120, cap=0.99)), ("15m", dict(z0=None, lead_bp=24, x_hi=297, x_lo=177, cap=0.99, pmin=0.97))):
    ent, s = run(kind, **args); print("\n" + s)
    def pn(e): return (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
    by = collections.defaultdict(list)
    for e in ent: by[time.strftime("%m-%d", time.gmtime(e["t"]))].append(e)
    print("  by day (n/lost/pnl$ at 10 sh):", " ".join(f"{d}:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum(pn(e) for e in v):+.1f}" for d, v in sorted(by.items())))
    by = collections.defaultdict(list)
    for e in ent: by[e["asset"]].append(e)
    print("  by asset:", " ".join(f"{a}:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum(pn(e) for e in v):+.1f}" for a, v in sorted(by.items())))
    by = collections.defaultdict(list)
    for e in ent: by[e["x"] // 60].append(e)
    print("  by minutes left:", " ".join(f"{m}-{m+1}m:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum(pn(e) for e in v):+.1f}" for m, v in sorted(by.items())))
    by = collections.defaultdict(list)
    for e in ent: by[round(e["px"], 2)].append(e)
    print("  by price:", " ".join(f"{p}:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum(pn(e) for e in v):+.1f}" for p, v in sorted(by.items())))
    # concurrency: entries whose windows overlap in time (held from t to the window end)
    ends = collections.Counter()
    for e in ent: ends[e["t"] + e["x"]] += 1
    print("  entries per close time:", sorted(collections.Counter(ends.values()).items()))
