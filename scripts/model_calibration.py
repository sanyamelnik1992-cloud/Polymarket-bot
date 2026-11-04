"""Calibrate the TWAP fair-value model on a per-second grid (independent of trade selection).

usage: python scripts/model_calibration.py HIST_DIR
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import load  # noqa: E402
from pmbot.model import prob_up  # noqa: E402


def grid(mk, kl, halflife=300, vol_mult=1.0, df_t=None, step=5, obs_lag=1):
    rows = []
    for a, k in kl.items():
        c = k["c"]
        lr = np.log(c).diff()
        sig = np.sqrt((lr ** 2).ewm(halflife=halflife, min_periods=60).mean()) * vol_mult
        roll = c.rolling(60, min_periods=60).mean()
        cs = c.cumsum()
        sub = mk[mk.asset == a]
        for rel in range(step, 300, step):
            T = sub.window_start.values
            ts = T + rel - obs_lag
            npart = np.clip(ts - (T + 240), 0, 60)
            psum = np.where(npart > 0, cs.reindex(ts).values - cs.reindex(T + 240).values, 0.0)
            p = prob_up(rel - obs_lag, c.reindex(ts).values, roll.reindex(T).values, sig.reindex(ts).values,
                        psum, npart, df=df_t)
            rows.append(pd.DataFrame({"asset": a, "rel": rel, "p": p, "y": (sub.winner == "Up").values.astype(float)}))
    g = pd.concat(rows).dropna()
    return g


def score(g):
    p = g.p.clip(1e-4, 1 - 1e-4)
    return float(-(g.y * np.log(p) + (1 - g.y) * np.log(1 - p)).mean()), float(((p - g.y) ** 2).mean())


if __name__ == "__main__":
    mk, tr, kl = load(sys.argv[1])
    print("variant                         logloss  brier")
    best = None
    for hl in (60, 300, 900):
        for vm in (0.8, 1.0, 1.25, 1.5):
            for df_t in (None, 4):
                g = grid(mk, kl, hl, vm, df_t, step=10)
                ll, br = score(g)
                print(f"hl={hl:4d} vol_mult={vm:4.2f} df={str(df_t):4s}   {ll:.4f}  {br:.4f}")
                if best is None or ll < best[0]:
                    best = (ll, hl, vm, df_t)
    print("best", best)
    _, hl, vm, df_t = best
    g = grid(mk, kl, hl, vm, df_t, step=5)
    g["pb"] = pd.cut(g.p, [0, .05, .15, .3, .45, .55, .7, .85, .95, 1])
    print(g.groupby("pb", observed=True).agg(n=("y", "size"), p=("p", "mean"), win=("y", "mean")).round(4))
    g["tb"] = pd.cut(g.rel, [0, 60, 120, 180, 240, 270, 290, 300])
    print(g.groupby("tb", observed=True).apply(lambda x: pd.Series(dict(zip(["logloss", "brier"], score(x))))).round(4))
