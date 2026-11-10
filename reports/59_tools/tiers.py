"""Tables of reports/59: the live rule by tier, the rule without the bottom tier(s), a stricter bottom tier, the trend filter.
usage: REPLAY_GRID=dir python reports/59_tools/tiers.py   (grids: reports/52_tools/grid.py, features: feat.py, Gamma outcomes)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import *
g = load_all()
def both(name, e):
    print(line(name, e)); print(line("    with one entry at a time ($5 open)", limits(e, 5, 1e9)))
    return e
e = both("live rule now (4 tiers)", run(*live_rule(g)))
e["tier"] = tier_of(e.p.values)
for t, x in e.groupby("tier"): print(line("    tier " + t, x))
print("-- drop tiers")
both("3 tiers: p>=.95 (no .90 tier)", run(*live_rule(g, tiers=L3[:3])))
both("2 tiers: p>=.97", run(*live_rule(g, tiers=L3[:2])))
print("-- stricter bottom tier")
for pm in (0.91, 0.92, 0.93, 0.94):
    both(f"bottom tier from p>={pm}", run(*live_rule(g, tiers=L3[:3] + [[pm, 0.91]])))
for am in (0.90, 0.89):
    both(f"bottom tier ask<={am}", run(*live_rule(g, tiers=L3[:3] + [[0.90, am]]), a_min=0.88))
print("-- trend filter on the bottom tier only (20 s move against the side, Binance)")
for thr in (1.0, 1.5, 2.0, 3.0):
    both(f"p<.95 needs tr_bn20 <= {thr} bp", run(*live_rule(g, extra=lambda G, thr=thr: (G.p >= .95) | (G.tr_bn20 <= thr))))
print("-- the same without the floor and the pause (more entries, more losses): is p>=.95 safer there too?")
for nm, tiers in (("plain L3, 4 tiers", L3), ("plain L3, 3 tiers", L3[:3]), ("plain L3, 2 tiers", L3[:2])):
    ok = base_ok(g.assign(p=g.p_raw, p_cl=g.pcl_raw)) & (g.p_raw >= min(t[0] for t in tiers))
    x = run(g.assign(p=g.p_raw, p_cl=g.pcl_raw), ok, tier_limit(g.p_raw.values, tiers)); print(line(nm, x))
    x["tier"] = tier_of(x.p.values)
    for t, y in x.groupby("tier"): print(line("    tier " + t, y))
print("-- by period, 3 tiers vs 4 tiers ($5 open)")
for nm, tiers in (("4", L3), ("3", L3[:3])):
    x = limits(run(*live_rule(g, tiers=tiers)), 5, 1e9)
    print(nm, " ".join(f"{p}: {int((x.period==p).sum())}/{int(((x.period==p)&~x.won).sum())} {x[x.period==p].pnl.sum():+.1f}" for p in PER))
