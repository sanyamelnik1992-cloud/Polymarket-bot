"""Table of reports/52: L3 with a sigma floor of 0 .. 0.8 bp on the state grids, without limits and with the live limits.
usage: REPLAY_GRID=dir python reports/52_tools/floor_scan.py"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sim import L3, base_ok, limits, load, p_sigma, run, run_delay, summ, tier_limit  # noqa: E402

g = load()
for F in (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8):
    s = np.maximum(g.sigma_bp.values, F)
    gg = g.assign(p=p_sigma(g, s), p_cl=p_sigma(g, s, "cl"))
    ok = base_ok(gg) & (gg.p >= 0.90)
    e = run(gg, ok, tier_limit(gg.p.values, L3))
    print(summ(e, f"floor {F} no limits"))
    print(summ(limits(e, 5), f"floor {F} live limits"))
    if F in (0.0, 0.5):
        print(summ(run_delay(gg, ok, tier_limit(gg.p.values, L3), exec_ms=1000, persist=True), f"floor {F} ask must live 1 s"))
