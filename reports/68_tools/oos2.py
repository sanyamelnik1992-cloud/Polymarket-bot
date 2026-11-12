"""Out-of-sample selection under three assumptions about empty markets. Rank on the first half of the week (reward per $ from the
official competitiveness, minus past fills x cost), trade the second half. usage: oos2.py SIM.pkl"""
import sys
import numpy as np, pandas as pd
o = pd.read_pickle(sys.argv[1])
COST = 0.05
def half(d, touch, h):
    k = o[(o.d == d) & (o.touch == touch) & (o.half == h)].set_index("cid")
    k = k.assign(rpd=k.rew / k.days, lpd=k.loss / k.days, fpd=k.fills / k.days, tpd=k.ntr / k.days)
    k["npd"] = k.rpd - k.lpd
    return k
def table(d, universe, label, min_rew=1.0, share_cap=1.0, budgets=(300, 500, 1000, 2000, 5000)):
    a, b = half(d, 1, 0), half(d, 1, 1)
    idx = a.index.intersection(b.index); a, b = a.loc[idx], b.loc[idx]
    Q = a.S * ((a.v - d) / a.v) ** 2
    share = Q / (a.comp * a.rate + Q)
    share = np.minimum(share, share_cap)
    rew_now = a.rate * share
    keep = universe(a) & (rew_now >= min_rew)
    a, b, rew_now, share = a[keep], b[keep], rew_now[keep], share[keep]
    # realised reward in the second half with the same share cap
    scale = np.where(rew_now > 0, np.minimum(1.0, (a.rate * share) / (a.rate * (Q[keep] / (a.comp * a.rate + Q[keep])))), 1.0)
    rpd2 = b.rpd * scale
    pred = (rew_now - a.fpd * a.S * COST) / a.S
    order = pred.sort_values(ascending=False).index
    print(f"\n{label}: d = {d:g}c, {len(a)} eligible markets (expected reward >= ${min_rew:g}/day each)")
    print(f"  {'budget':>7s} {'markets':>7s} {'reward/d':>9s} {'loss/d':>7s} {'net/d':>7s} {'net %/d':>8s} {'fills/d':>7s} {'net>0':>6s} {'worst mkt':>9s} {'med pool':>8s} {'med comp':>8s}")
    for budget in budgets:
        cum = a.S.loc[order].cumsum(); pick = order[(cum <= budget).values]
        if not len(pick): continue
        r, l = rpd2.loc[pick], b.lpd.loc[pick]; n = r - l
        print(f"  {budget:7d} {len(pick):7d} {r.sum():9.1f} {l.sum():7.1f} {n.sum():7.1f} {100*n.sum()/a.S.loc[pick].sum():7.1f}% {b.fpd.loc[pick].sum():7.1f} {100*(n > 0).mean():5.0f}% {n.min():9.2f} {a.rate.loc[pick].median():8.1f} {a.comp.loc[pick].median():8.2f}")
for d in (2.0, 3.0):
    table(d, lambda a: a.comp >= 0, "A. formula as documented (a lone maker takes the whole pool)")
    table(d, lambda a: a.comp > 0, "B. only markets where somebody already scores (competitiveness > 0)")
    table(d, lambda a: a.comp >= 0, "C. as A, but our share of any pool capped at 25 %", share_cap=0.25)
    table(d, lambda a: a.comp > 1, "D. only clearly contested markets (competitiveness > 1)")
