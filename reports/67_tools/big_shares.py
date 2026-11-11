"""10 shares instead of 5 on the upper tiers of live 5m L3: replay of 7-11 Nov with the live limits (one window at a time,
daily stop), with and without the insurance of reports/64. Entries: the first fill of a window (run() of reports/52_tools/sim.py,
book 300 ms later, 2 levels), 10 shares when the model p >= BIG_P, else 5.
usage: REPLAY_GRID=<grids GRID_P_MIN=0.5> python reports/67_tools/big_shares.py"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "64_tools"))
from hedge_sim import hedge, load_live, run  # noqa: E402

PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
g, ok, lim = load_live(PER)


def entries(big_p, big=10.0):
    if big_p is None:
        e = run(g, ok, lim)
    else:
        hi = run(g, ok & (g.p >= big_p - 1e-12), lim, shares=big)
        lo = run(g, ok & (g.p < big_p - 1e-12), lim, shares=5.0)
        e = pd.concat([hi, lo], ignore_index=True)
    return e.sort_values("t").groupby(["asset", "T"], sort=False).head(1).sort_values("t").reset_index(drop=True)


def live_limits(e, day_stop):
    """One window at a time (all windows of a cycle end together: one entry per cycle) and the daily stop on realized P&L."""
    keep, used, day_pnl, killed = [], set(), {}, set()
    for r in e.itertuples():
        day = pd.Timestamp(r.T + 300, unit="s").strftime("%m-%d")
        if day in killed or r.T in used:
            continue
        if sum(p for end, p in day_pnl.get(day, []) if end * 1000 <= r.t) <= -day_stop:
            killed.add(day)
            continue
        used.add(r.T)
        day_pnl.setdefault(day, []).append((r.T + 300, r.pnl_x))
        keep.append(r.Index)
    return e.loc[keep]


rows = []
for name, big_p, stop in (("now: 5 shares, stop -$5", None, 5.0), ("10 sh from p 0.97, stop -$5", 0.97, 5.0),
                          ("10 sh from p 0.97, stop -$10", 0.97, 10.0), ("10 sh from p 0.99, stop -$5", 0.99, 5.0),
                          ("10 sh from p 0.99, stop -$10", 0.99, 10.0)):
    e = entries(big_p)
    for ins in (False, True):
        x = hedge(e, g, 0.65, lambda p: p - 0.20, slip=0.02) if ins else e.assign(pnl_h=e.pnl)
        x = x.assign(pnl_x=x.pnl_h)
        y = live_limits(x, stop)
        y = y.assign(day=pd.to_datetime(y["T"] + 300, unit="s").dt.strftime("%m-%d"))
        by_day = y.groupby("day").pnl_x.sum()
        by_per = y.groupby("period").pnl_x.sum()
        rows.append({"rule": name + (" + insurance" if ins else ""), "entries": len(y), "10-share": int((y.sh > 5.5).sum()),
                     "lost": int((y.pnl_x < 0).sum()), "pnl": round(y.pnl_x.sum(), 2), "worst trade": round(y.pnl_x.min(), 2),
                     "worst day": round(by_day.min(), 2), "days<0": int((by_day < 0).sum()), "periods<0": int((by_per < 0).sum()),
                     "by day": " ".join(f"{v:+.1f}" for v in by_day.values)})
pd.set_option("display.width", 250)
print(pd.DataFrame(rows).to_string(index=False))
