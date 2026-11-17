"""The paper bots' rule exactly (sigma from 10 s returns over 1 h): 5m z>=3 at 120-62 s + 15m z>=3 at 300-120 s, asks 0.985-0.99.
Combined by day at 10 shares, drawdown, losses per day."""
import time, collections
import zmod; zmod.STEP = 10
from sim import run, fee
def pn(e): return (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
e5, s5 = run("5m", z0=3, x_hi=120, x_lo=62, cap=0.99, pmin=0.985); print(s5)
e15, s15 = run("15m", z0=3, x_hi=300, x_lo=120, cap=0.99, pmin=0.985); print(s15)
allx = sorted(e5 + e15, key=lambda e: e["t"])
by = collections.defaultdict(float); n = collections.Counter(); lo = collections.Counter()
for e in allx:
    d = time.strftime("%m-%d", time.gmtime(e["t"])); by[d] += 10 * pn(e); n[d] += 1; lo[d] += (not e["won"])
print("by day (entries/lost/$ at 10 sh):", " ".join(f"{d}:{n[d]}/{lo[d]}/{v:+.1f}" for d, v in sorted(by.items())), "total %+.1f" % sum(by.values()))
eq = mx = dd = 0.0
for e in allx:
    eq += 10 * pn(e); mx = max(mx, eq); dd = max(dd, mx - eq)
print("max drawdown $%.2f, positive days %d of %d" % (dd, sum(1 for v in by.values() if v > 0), len(by)))
