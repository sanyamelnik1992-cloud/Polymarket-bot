"""Tables of reports/55: plain L3, the sigma floor, the top tier on the unfloored model, the pause after a Binance jump.
usage: REPLAY_GRID=dir python reports/55_tools/union.py   (grids from reports/52_tools/grid.py, outcomes patched from Gamma)"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, "reports/52_tools")
from sim import L3, base_ok, limits, load, p_sigma, run, tier_limit
g = load()
PER = sorted(g.period.unique())
s = np.maximum(g.sigma_bp.values, 0.5)
g["p_raw"], g["pcl_raw"] = g.p, g.p_cl
pf, pcf = p_sigma(g, s), p_sigma(g, s, "cl")
def rep(name, e):
    out = f"{name:44s}"
    for lab, x in (("nolim", e), ("$5", limits(e, 5) if len(e) else e)):
        if not len(x): out += " | n=0"; continue
        out += f" | {lab} n={len(x):3d} L={int((~x.won).sum()):2d} {100*x.won.mean():5.1f}% {x.pnl.sum():+6.2f} "
        out += " ".join(f"{int((x.period==p).sum())}/{int(((x.period==p)&~x.won).sum())}" for p in PER)
    print(out); return e
def rule(top=0.99, shock=None, shock_all=True, look=10, floor=True):
    use_raw = (g.p_raw.values >= top) if top else np.zeros(len(g), bool)
    G = g.assign(p=np.where(use_raw | (not floor), g.p_raw, pf), p_cl=np.where(use_raw | (not floor), g.pcl_raw, pcf))
    ok = base_ok(G) & (G.p >= .9)
    if shock:
        sh = (g[f"sh3_{look}"] <= shock)
        ok = ok & (sh | (False if shock_all else use_raw))
    return G, ok, tier_limit(G.p.values, L3)
print(PER)
rep("plain L3 (till 10 Nov 08:14)", run(*rule(top=0, floor=False)))
rep("floor (live now)", run(*rule(top=0)))
rep("floor, top tier on the raw model", run(*rule()))
for k in (2.0, 2.5, 3.0, 4.0):
    rep(f"  + shock {k} bp/10 s on everything", run(*rule(shock=k)))
    rep(f"  + shock {k} bp/10 s, not on the top tier", run(*rule(shock=k, shock_all=False)))
G, ok, lim = rule(shock=2.5)
e = limits(run(G, ok, lim), 5)
print(e.assign(day=pd.to_datetime(e["T"], unit="s").dt.strftime("%m-%d %H").str[:5]).groupby("day").agg(n=("won","size"), lost=("won", lambda s:int((~s).sum())), pnl=("pnl","sum")).round(2).T.to_string())
print(e[~e.won][["period","asset","T","rel","p","p_raw","dist_bp","sigma_bp","sh3_10","ask","px"]].round(3).to_string())
