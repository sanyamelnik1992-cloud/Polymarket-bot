"""Like bt.py, with a sigma floor and/or a long-lookback sigma. Prints N windows / lost for z >= z0, and windows per day."""
import json, collections, numpy as np, sys
from zmod import *
W = json.load(open("windows.json"))
FLOOR = float(sys.argv[1]); LOOK = int(sys.argv[2])
days = 8.0
def zf(w, t):
    lead, sg, _ = z_at(w, t)
    s = max(sigma(w["asset"], t, LOOK), FLOOR)
    return lead, lead / (s * np.sqrt(tau(w, t)))
X = {"5m": [150, 120, 90, 62], "15m": [420, 300, 240, 180, 120, 62], "1h": [900, 600, 420, 300, 180, 120, 60]}
ZB = [2.5, 3, 3.5, 4, 5]
print(f"sigma floor {FLOOR} bp/sqrt(s), lookback {LOOK} s")
for kind in ("5m", "15m", "1h"):
    ws = [w for w in W if w["kind"] == kind]
    print(f"{kind}: N(z>=z0) / lost  [per day]")
    for x in X[kind]:
        zs = []
        for w in ws:
            lead, z = zf(w, w["end"] - x)
            zs.append((abs(z), ("Up" if lead > 0 else "Down") != w["win"]))
        cells = []
        for z0 in ZB:
            n = sum(1 for z, l in zs if z >= z0); lo = sum(1 for z, l in zs if z >= z0 and l)
            cells.append(f"{n:5d}/{lo:<2d}[{n/days:5.1f}]")
        print(f"  x={x:4d} " + " ".join(f"z>={z0}: {c}" for z0, c in zip(ZB, cells)))
