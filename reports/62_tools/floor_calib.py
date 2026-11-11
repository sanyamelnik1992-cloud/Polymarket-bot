"""Reports/62: is the live L3 rule (3 tiers, sigma floor 0.5, floor-free 0.99, shock pause) worse in a calm market?

Part 1: the rule with sigma floors 0.5 / 0.6 / 0.65 / 0.7 by period, and the windows a 0.6 floor drops.
Part 2: calibration on ALL windows (no ask condition): the first state from second 240 where the floored model reaches
p in a tier band and the state filters pass; realized loss rate vs the model's, by the raw 15-min sigma. The entries are
a few dozen; the windows are about a thousand, so part 2 is the one that can tell a calm-market defect from bad luck.

usage: REPLAY_GRID=dir python reports/62_tools/floor_calib.py
(grids: reports/52_tools/grid.py, features: reports/59_tools/feat.py, Gamma outcomes: reports/52_tools/gamma_outcomes.py)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "59_tools"))
import base  # noqa: E402
from base import L3, base_ok, line, load_all, np, p_sigma, run, tier_limit, tier_of  # noqa: E402

base.PER[:] = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111"]
g = load_all()
T3 = L3[:3]


def rule(g, floor=0.5, tiers=T3):
    s = np.maximum(g.sigma_bp.values, floor)
    raw = g.p_raw.values >= 0.99
    G = g.assign(p=np.where(raw, g.p_raw, p_sigma(g, s)), p_cl=np.where(raw, g.pcl_raw, p_sigma(g, s, "cl")))
    su = np.where(raw, g.sigma_bp, s)
    ok = base_ok(G) & (G.p >= min(t[0] for t in tiers)) & (g.sh3_10 <= np.maximum(2.5, 3 * su * np.sqrt(3)))
    return run(G, ok, tier_limit(G.p.values, tiers))


print("== 1. the live rule with other sigma floors (entries/lost pnl, 5 shares, no limits)")
E = {f: rule(g, f) for f in (0.5, 0.6, 0.65, 0.7)}
print("period    " + "".join(f"{'floor ' + str(k):>20s}" for k in E))
for p in base.PER + ["ALL"]:
    cells = []
    for e in E.values():
        x = e if p == "ALL" else e[e.period == p]
        cells.append(f"{len(x):3d}/{int((~x.won).sum())} {x.pnl.sum():+7.2f}".rjust(20))
    print(f"{p:10s}" + "".join(cells))
a, b = E[0.5], E[0.6]
kb, ka = set(zip(b.asset, b["T"])), set(zip(a.asset, a["T"]))
print(line("windows floor 0.6 drops", a[[k not in kb for k in zip(a.asset, a["T"])]]))
print(line("windows floor 0.6 adds", b[[k not in ka for k in zip(b.asset, b["T"])]]))
a["tier"] = tier_of(a.p.values)
x = a[a.tier != "1 p>=.99"]
print("-- live rule entries, tiers 2-3, by raw 15-min sigma (bp)")
for lo, hi in ((0, .4), (.4, .5), (.5, .6), (.6, .8), (.8, 99)):
    y = x[(x.sigma_bp >= lo) & (x.sigma_bp < hi)]
    print(line(f"   sigma [{lo},{hi})", y), " model expects lost:", round(float((1 - y.p).sum()), 2))


def first(g, floor, lo, hi):
    s = np.maximum(g.sigma_bp.values, floor)
    G = g.assign(p=p_sigma(g, s), p_cl=p_sigma(g, s, "cl"))
    ok = base_ok(G) & (G.p >= lo) & (G.p < hi) & (G.rel >= 240) & (G.p_raw < 0.99) & \
        (g.sh3_10 <= np.maximum(2.5, 3 * s * np.sqrt(3)))
    return G[ok].sort_values("t").groupby(["asset", "T"], sort=False).head(1)


print("== 2. calibration on all windows (first state in the band, any ask)")
for floor in (0.5, 0.6):
    for lo, hi in ((0.95, 0.97), (0.97, 0.99)):
        d = first(g, floor, lo, hi)
        print(f"floor {floor}, p in [{lo},{hi}): n={len(d)} lost={int((~d.won).sum())} ({100 * (~d.won).mean():.1f}%)"
              f" model {100 * (1 - d.p).mean():.1f}%")
        for s0, s1 in ((0, .4), (.4, .5), (.5, .6), (.6, .8), (.8, 1.2), (1.2, 99)):
            y = d[(d.sigma_bp >= s0) & (d.sigma_bp < s1)]
            if len(y):
                print(f"   raw sigma [{s0},{s1}): n={len(y):4d} lost={int((~y.won).sum()):3d} ({100 * (~y.won).mean():4.1f}%)"
                      f"  model {100 * (1 - y.p).mean():.1f}%")
