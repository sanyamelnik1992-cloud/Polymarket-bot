"""Second and third entries of a window: profitable on their own? Same side as the first? Gap after the previous entry."""
import sys, collections, math
sys.path.insert(0, sys.argv[1])
from fv2 import events, pick, rep, fee
import numpy as np
for kind, xhi in (("5m", 290), ("15m", 840)):
    ev = events(kind, 1)
    for gap in (0, 5, 15, 30):
        sel, last, cnt, first_side = [], {}, collections.Counter(), {}
        for e in ev:
            if e["edge"] < 0.15 or not 0.05 <= e["px"] <= 0.95 or e["sd"] != "B" or not 62 <= e["x"] <= xhi: continue
            s = e["slug"]
            if cnt[s] >= 3 or (s in last and e["t"] - last[s] < gap): continue
            e = dict(e, k=cnt[s] + 1, same=(first_side.get(s, e["side"]) == e["side"]))
            first_side.setdefault(s, e["side"]); cnt[s] += 1; last[s] = e["t"]; sel.append(e)
        for k in (1, 2, 3):
            rep([e for e in sel if e["k"] == k], f"{kind} gap>={gap:2d}s entry #{k}")
        rep([e for e in sel if e["k"] > 1 and e["same"]], f"{kind} gap>={gap:2d}s later, same side")
        rep([e for e in sel if e["k"] > 1 and not e["same"]], f"{kind} gap>={gap:2d}s later, other side")
        # per-window clustered t of the whole selection
        cl = collections.defaultdict(float)
        for e in sel: cl[e["slug"]] += (1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])
        xs = np.array(list(cl.values())); print(f"   windows {len(xs)}, clustered t {xs.mean() / xs.std(ddof=1) * math.sqrt(len(xs)):+.2f}")
