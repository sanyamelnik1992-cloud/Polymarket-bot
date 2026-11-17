"""combo.py with sigma from 10 s returns (STEP=10), several z thresholds; by asset."""
import json, time, collections, sys
import zmod; zmod.STEP = int(sys.argv[1]) if len(sys.argv) > 1 else 10
from sim import run, fee
def pn(e): return (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
for kind, z0, xh, xl in (("5m", 2.5, 120, 62), ("5m", 3, 120, 62), ("5m", 3.5, 120, 62), ("15m", 2.5, 300, 120), ("15m", 3, 300, 120), ("15m", 3.5, 300, 120)):
    ent, s = run(kind, z0=z0, x_hi=xh, x_lo=xl, cap=0.99, pmin=0.985)
    by = collections.defaultdict(list)
    for e in ent: by[e["asset"]].append(e)
    days = collections.defaultdict(float)
    for e in ent: days[time.strftime("%m-%d", time.gmtime(e["t"]))] += 10 * pn(e)
    print(s, "| neg days", sum(1 for v in days.values() if v < 0), "|", " ".join(f"{a}:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum(pn(e) for e in v):+.1f}" for a, v in sorted(by.items())), flush=True)
