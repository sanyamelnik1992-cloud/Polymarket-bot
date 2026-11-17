"""Expected losses of the competitor's entries from the market-wide loss rate at the same kind, time left and z (1 h sigma)."""
import json, numpy as np, collections
from zmod import *
W = json.load(open("windows.json"))
G = {"5m": range(60, 190, 10), "15m": range(90, 430, 15), "1h": range(120, 920, 30)}
grid = collections.defaultdict(list)          # kind -> [(x, |z|, lost)]
for w in W:
    for x in G[w["kind"]]:
        t = w["end"] - x
        lead, _, _ = z_at(w, t)
        z = lead / (sigma(w["asset"], t, 3600) * np.sqrt(tau(w, t)))
        grid[w["kind"]].append((x, abs(z), ("Up" if lead > 0 else "Down") != w["win"]))
G2 = {k: np.array(v, dtype=float) for k, v in grid.items()}
np.savez_compressed("grid.npz", **G2)
C = json.load(open("comp_z.json"))
for kind, dx in (("5m", 20), ("15m", 45), ("1h", 90)):
    g = G2[kind]; tot = 0.0; n = 0
    for c in (c for c in C if c["kind"] == kind):
        m = (np.abs(g[:, 0] - c["left"]) <= dx) & (np.abs(g[:, 1] - c["z"]) <= 0.25)
        if m.sum() >= 20:
            tot += g[m, 2].mean(); n += 1
    lost = sum(not c["won"] for c in C if c["kind"] == kind)
    print(f"{kind}: his entries {sum(1 for c in C if c['kind']==kind)}, matched {n}, expected losses {tot:.2f}, actual {lost}")
