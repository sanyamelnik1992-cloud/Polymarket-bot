"""The live L3 rule of 10 Nov 16:45 (floor 0.5 bp, top tier on the unfloored model, shock pause max(2.5 bp, 3 sigma sqrt 3))
on the state grids + the trend features of feat.py. Import: from base import *; G, ok, lim = live_rule()."""
import glob, os, sys
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "52_tools"))
from sim import GRID, L3, base_ok, limits, load, p_sigma, run, tier_limit  # noqa: E402
PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c"]
def load_all():
    g = load(PER)
    f = pd.concat([pd.read_parquet(os.path.join(GRID, "feat", p + ".parquet")) for p in sorted(PER)], ignore_index=True)
    assert len(f) == len(g)
    g = pd.concat([g, f], axis=1)
    order = {p: i for i, p in enumerate(PER)}
    first = g.assign(o=g.period.map(order)).groupby(["asset", "T"]).o.transform("min")
    g = g[g.period.map(order) == first].reset_index(drop=True)      # periods overlap by a few windows
    s = np.maximum(g.sigma_bp.values, 0.5)
    g["p_raw"], g["pcl_raw"] = g.p, g.p_cl
    g["pf"], g["pcf"] = p_sigma(g, s), p_sigma(g, s, "cl")
    return g
def live_rule(g, tiers=L3, shock=True, extra=None):
    raw = g.p_raw.values >= 0.99
    G = g.assign(p=np.where(raw, g.p_raw, g.pf), p_cl=np.where(raw, g.pcl_raw, g.pcf))
    G["sig_used"] = np.where(raw, g.sigma_bp, np.maximum(g.sigma_bp, 0.5))
    ok = base_ok(G) & (G.p >= min(t[0] for t in tiers))
    if shock: ok = ok & (g.sh3_10 <= np.maximum(2.5, 3 * G.sig_used * np.sqrt(3)))
    if extra is not None: ok = ok & extra(G)
    return G, ok, tier_limit(G.p.values, tiers)
def tier_of(p): return np.select([p >= .99, p >= .97, p >= .95], ["1 p>=.99", "2 .97-.99", "3 .95-.97"], "4 .90-.95")
def line(name, e):
    if not len(e): return f"{name:46s} n=0"
    L = int((~e.won).sum())
    return f"{name:46s} n={len(e):3d} lost={L:2d} ({100*L/len(e):4.1f}%) pnl={e.pnl.sum():+7.2f} c/sh={100*e.pnl.sum()/e.sh.sum():+5.2f}"
