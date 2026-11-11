"""Which insurance purchases were for nothing (the original side won after all)? Features at the hedge moment, pooled over the
real entries of 10-11 Nov and the wider replay rules (plain L3 with four tiers, L4)."""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hedge_sim import PER, base_ok, fill, limits, load, load_live, run, tier_limit, L3  # noqa: E402
from hedge_eval import journal_entries  # noqa: E402


def hedges(e, g, H=0.65, retry_ms=1000):
    idx = {k: w.sort_values("t") for k, w in g.groupby(["asset", "T"], sort=False)}
    rows = []
    for r in e.itertuples():
        w = idx[(r.asset, r.T)]
        w = w[(w.t >= r.t + 300) & (w.side != r.side) & (w.p >= H)]
        nt = 0
        for x in w.itertuples():
            if x.t < nt:
                continue
            got, cost, fee = fill(x, x.p, r.sh)
            if got > 0:
                rows.append({"src": r.src, "won_orig": r.won, "entry_px": r.px, "entry_rel": r.rel, "rel": x.rel, "p": x.p, "p_cl": x.p_cl,
                             "h_px": cost / got, "sigma": x.sigma_bp, "dist": x.dist_bp, "adv3": x.adv3, "m": x.m,
                             "gain": ((got if not r.won else 0.0) - cost - fee)})
                break
            nt = x.t + retry_ms
    return pd.DataFrame(rows)


g, ok, lim = load_live(PER)
je = journal_entries(g, os.path.join(HERE, "entries_10_11nov.json")).assign(src="real 10-11 Nov")
parts = [hedges(je, g)]
g0 = load(PER)
order = {p: i for i, p in enumerate(PER)}
first = g0.assign(o=g0.period.map(order)).groupby(["asset", "T"]).o.transform("min")
g0 = g0[g0.period.map(order) == first].reset_index(drop=True)
okw = base_ok(g0) & (g0.p >= 0.90)
e = limits(run(g0, okw, tier_limit(g0.p.values, L3), a_min=0.50).sort_values("t"), 5, 1e9).reset_index(drop=True)
e["src"] = np.where(e.period.isin(["r1107", "r1108", "r1109", "d1109", "d1110"]), "L4 replay 7-9 Nov", "L4 replay 10-11 Nov")
parts.append(hedges(e, g0))
h = pd.concat(parts, ignore_index=True)
h["false"] = h.won_orig
print(f"{len(h)} insurance purchases, for nothing: {int(h['false'].sum())}; gain of the purchase itself (vs holding) {h.gain.sum():+.2f}")
print(h.groupby("src").apply(lambda w: pd.Series({"n": len(w), "for nothing": int(w["false"].sum()), "O won %": round(100 * (1 - w["false"].mean())),
      "mean price": round(w.h_px.mean(), 3), "mean model p": round(w.p.mean(), 3), "gain $": round(w.gain.sum(), 2)})).to_string())
for col, bins in (("rel", [239, 260, 270, 280, 290, 300]), ("h_px", [0, 0.4, 0.5, 0.6, 0.7, 0.8, 1]), ("p_cl", [0, 0.5, 0.6, 0.7, 0.8, 1.01]),
                  ("p", [0.6, 0.7, 0.8, 0.9, 1.01]), ("sigma", [0, 0.5, 0.7, 1.0, 10])):
    print(f"-- by {col}")
    for k, w in h.groupby(pd.cut(h[col], bins), observed=True):
        print(f"   {str(k):14s} n {len(w):3d} for nothing {int(w['false'].sum()):3d} ({100*w['false'].mean():3.0f}%)  price {w.h_px.mean():.2f}  model {w.p.mean():.2f}  gain {w.gain.sum():+6.2f}")
