"""The exchange averages one second later in about a third of the windows (reports/52). Rule variant: the model probability is
the smaller of the two windows' ([T+238, T+297] and [T+239, T+298], strikes [T-62, T-3] / [T-61, T-2]). Replay of the live rule.
usage: REPLAY_GRID=<grids GRID_P_MIN=0.5> python reports/65_tools/window_shift_rule.py"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "64_tools"))
sys.path.insert(0, os.path.join(HERE, "..", "52_tools"))
from hedge_sim import limits, load_live, run  # noqa: E402
from sim import L3, base_ok, p_sigma, tier_limit  # noqa: E402

PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
g, ok, lim = load_live(PER)
raw = g.p_raw.values >= 0.99
s = np.where(raw, g.sigma_bp.values, np.maximum(g.sigma_bp.values, 0.5))
sh = g.assign(y_nc=g.y1_nc, m_nc=g.m1_nc)
ok1 = sh.y_nc.notna() & (sh.m_nc > 0)
p1 = np.where(ok1, p_sigma(sh.fillna({"y_nc": 0.0, "m_nc": 1}), s), g.p)
g["p_shift"] = p1
for name, pcol in (("live rule", "p"), ("p = min(both windows)", "pmin")):
    G = g.assign(pmin=np.minimum(g.p, g.p_shift))
    G["p"] = G[pcol]
    su = np.where(raw, g.sigma_bp, np.maximum(g.sigma_bp, 0.5))
    okk = base_ok(G) & (G.p >= 0.95) & (G.sh3_10 <= np.maximum(2.5, 3 * su * np.sqrt(3)))
    e = limits(run(G, okk, tier_limit(G.p.values, L3[:3])).sort_values("t"), 5, 1e9)
    per = e.groupby("period").pnl.sum()
    print(f"{name:26s} n {len(e):3d} lost {int((~e.won).sum())} pnl {e.pnl.sum():+7.2f} periods<0 {int((per < 0).sum())}/9 worst {per.min():+.2f}")
print("windows where the two probabilities differ by > 1 pp at a live entry:",
      int((abs(g.p - g.p_shift) > 0.01)[ok & (g.ask <= lim)].groupby([g.asset, g["T"]]).any().sum()))
