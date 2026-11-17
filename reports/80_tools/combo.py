"""The proposed rule: 5m z>=3 at 62-120 s before the end + 15m z>=3 at 120-300 s, asks 0.985-0.99 only. By asset, by day, drawdown."""
import json, time, collections
from sim import run, fee
def pn(e): return (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
e5, s5 = run("5m", z0=3, x_hi=120, x_lo=62, cap=0.99, pmin=0.985); print(s5)
e15, s15 = run("15m", z0=3, x_hi=300, x_lo=120, cap=0.99, pmin=0.985); print(s15)
for name, ent in (("5m", e5), ("15m", e15)):
    by = collections.defaultdict(list)
    for e in ent: by[e["asset"]].append(e)
    print(f"  {name} by asset (n/lost/$ at 10 sh):", " ".join(f"{a}:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum(pn(e) for e in v):+.1f}" for a, v in sorted(by.items())))
allx = sorted(e5 + e15, key=lambda e: e["t"])
by = collections.defaultdict(float); n = collections.Counter(); lo = collections.Counter()
for e in allx:
    d = time.strftime("%m-%d", time.gmtime(e["t"])); by[d] += 10 * pn(e); n[d] += 1; lo[d] += (not e["won"])
print("combined by day ($ at 10 sh):", " ".join(f"{d}:{n[d]}/{lo[d]}/{v:+.1f}" for d, v in sorted(by.items())), " total %+.1f" % sum(by.values()))
eq = mx = dd = 0.0
for e in allx:
    eq += 10 * pn(e); mx = max(mx, eq); dd = max(dd, mx - eq)
print("max drawdown at 10 shares: $%.2f; worst single trade $%.2f" % (dd, min(10 * pn(e) for e in allx)))
