"""Loss rate of the leader by z and time before the end, all windows 9-16 Nov (Binance proxy vs Gamma outcomes)."""
import json, collections, numpy as np
from zmod import *
W = json.load(open("windows.json"))
# proxy accuracy
agree = collections.Counter()
for w in W:
    f, K = final(w), strike(w)
    side = "Up" if f >= K else "Down"
    agree[(w["kind"], side == w["win"])] += 1
print("Binance proxy agrees with Gamma:", {k: f"{agree[(k, True)]}/{agree[(k, True)] + agree[(k, False)]}" for k in ("5m", "15m", "1h")})
X = {"5m": [150, 120, 90, 62, 45, 30], "15m": [420, 300, 240, 180, 120, 62, 30], "1h": [900, 600, 420, 300, 180, 120, 60]}
ZB = [2, 2.5, 3, 3.5, 4, 5, 6]
for kind in ("5m", "15m", "1h"):
    ws = [w for w in W if w["kind"] == kind]
    print(f"\n{kind}: {len(ws)} windows. loss rate of the leader when z >= z0 (N with z >= z0 / lost)")
    print("  x(s)  " + "  ".join(f"z>={z:<4}" for z in ZB))
    for x in X[kind]:
        cells = []
        zs = []
        for w in ws:
            t = w["end"] - x
            lead, sg, z = z_at(w, t)
            lost = (("Up" if lead > 0 else "Down") != w["win"])
            zs.append((abs(z), lost))
        for z0 in ZB:
            n = sum(1 for z, l in zs if z >= z0); lo = sum(1 for z, l in zs if z >= z0 and l)
            cells.append(f"{n:5d}/{lo:<3d}")
        print(f"  {x:4d}  " + "  ".join(cells))
