"""Rule sensitivity with asks 0.985-0.99 only (a cheaper seller is a warning), and the losses: clustered or not."""
import json, time, collections
from sim import run, fee
def pn(e): return (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
for kind, grid in (("5m", [(2.5, 180, 62), (3, 180, 62), (3.5, 180, 62), (3, 240, 62), (3, 120, 62), (3, 180, 90)]),
                   ("15m", [(2.5, 420, 120), (3, 420, 120), (3.5, 420, 120), (3, 600, 120), (3, 420, 62), (3, 300, 120)]),
                   ("1h", [(2.5, 900, 60), (3, 900, 60), (3, 600, 60), (3.5, 900, 60)])):
    for z0, xh, xl in grid:
        ent, s = run(kind, z0=z0, x_hi=xh, x_lo=xl, cap=0.99, pmin=0.985)
        days = collections.Counter(time.strftime("%m-%d", time.gmtime(e["t"])) for e in ent)
        neg = sum(1 for d in days if sum(pn(e) for e in ent if time.strftime("%m-%d", time.gmtime(e["t"])) == d) < 0)
        print(s, f"| negative days {neg}/{len(days)}")
ent, s = run("5m", z0=3, x_hi=180, x_lo=62, cap=0.99, pmin=0.985)
print("\nlosses of 5m z>=3 x[62,180] at 0.99:")
for e in ent:
    if not e["won"]: print("  ", time.strftime("%m-%d %H:%M:%S", time.gmtime(e["t"])), e["slug"], "x", e["x"], "px", e["px"])
ent, s = run("15m", z0=3, x_hi=420, x_lo=120, cap=0.99, pmin=0.985)
print("losses of 15m z>=3 x[120,420] at 0.99:")
for e in ent:
    if not e["won"]: print("  ", time.strftime("%m-%d %H:%M:%S", time.gmtime(e["t"])), e["slug"], "x", e["x"], "px", e["px"])
