"""Windows already decided (z >= 3 with a 1 h sigma at the end of the early zone): was there a public buy of the leader at
0.985-0.99 in the early zone, and in the last minute? (report 80, section 4, finding 3)"""
import json, numpy as np
from zmod import *
W = json.load(open("windows.json")); MT = json.load(open("mtrades.json"))
for kind, early in (("5m", (180, 62)), ("15m", (300, 120))):
    n = a = b = 0
    for w in W:
        if w["kind"] != kind or w["slug"] not in MT: continue
        t = w["end"] - early[1]
        lead, _, _ = z_at(w, t)
        if abs(lead) / (sigma(w["asset"], t, 3600) * np.sqrt(tau(w, t))) < 3: continue
        side = "Up" if lead > 0 else "Down"; n += 1
        rows = [r for r in MT[w["slug"]] if r[1] == "B" and r[2] == side and 0.985 <= r[4] <= 0.99]
        a += any(w["end"] - early[0] <= r[0] <= w["end"] - early[1] for r in rows)
        b += any(w["end"] - 60 <= r[0] <= w["end"] - 3 for r in rows)
    print(f"{kind}: decided windows {n}; a buy at 0.985-0.99 early [{early[0]},{early[1]}] s: {a} ({100*a/n:.0f}%), last minute: {b} ({100*b/n:.0f}%)")
