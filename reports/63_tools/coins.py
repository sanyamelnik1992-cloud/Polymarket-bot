"""Report 63 tables on all periods: replay of the live rule with/without the BTC rule, per-coin calibration on all windows."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "59_tools"))
import base
from base import *
from scipy.stats import fisher_exact
PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
base.PER[:] = PER
g = load_all()
print("rows", len(g), "windows", g.groupby(["asset", "T"]).ngroups)
span = {p: (g[g.period == p]["T"].min(), g[g.period == p]["T"].max() + 300) for p in PER}
for p in PER:
    a, b = span[p]; print(f"  {p}: {pd.Timestamp(a, unit='s'):%m-%d %H:%M} - {pd.Timestamp(b, unit='s'):%m-%d %H:%M}")
res = {}
for nm, btc in (("live rule", None), ("+ BTC p>=0.97", 0.97)):
    extra = None if btc is None else (lambda G, b=btc: (G.asset != "btc") | (G.p >= b))
    G, ok, lim = live_rule(g, tiers=L3[:3], extra=extra)
    e = run(G, ok, lim).sort_values("t"); e["tier"] = tier_of(e.p.values)
    x = limits(e, 5, 1e9)
    res[nm] = (e, x)
    print(line(nm + " | no limits", e)); print(line(nm + " | one at a time", x))
print("per period, one at a time: entries / lost / pnl")
for p in PER:
    print(f"  {p:7s} " + "   ".join(f"{nm}: {len(w):3d} / {int((~w.won).sum())} / {w.pnl.sum():+6.2f}"
                                    for nm in res for w in [res[nm][1][res[nm][1].period == p]]))
e = res["live rule"][0]
print("live rule entries by coin x tier (no limits):")
for k, w in e.groupby([e.asset, e.tier.str[:1]]):
    print("  " + line(f"{k[0]} tier {k[1]}", w))
print("losses of the live rule:")
print(e[~e.won][["period", "asset", "T", "side", "rel", "p", "px", "sigma_bp", "dist_bp", "adv3", "pnl"]].to_string())
G, ok, lim = live_rule(g, tiers=L3[:3]); G = G[ok]
for lo, hi in ((0.95, 0.97), (0.97, 0.99), (0.99, 1.01)):
    s = G[(G.p >= lo) & (G.p < hi)].sort_values("t").groupby(["asset", "T"]).head(1)
    print(f"== all windows, first moment at p [{lo}, {hi}) with the live state filters, any ask")
    for a, w in s.groupby("asset"):
        print(f"   {a}: windows {len(w):4d} lost {int((~w.won).sum()):3d} ({100*(~w.won).mean():4.1f}%) model {100*(1-w.p).mean():.1f}%")
    b, o = s[s.asset == "btc"], s[s.asset != "btc"]
    print(f"   BTC vs rest: {int((~b.won).sum())}/{len(b)} vs {int((~o.won).sum())}/{len(o)}  Fisher p = "
          f"{fisher_exact([[int((~b.won).sum()), int(b.won.sum())], [int((~o.won).sum()), int(o.won.sum())]], alternative='greater')[1]:.3f}")
    for p in PER:
        bp, op = b[b.period == p], o[o.period == p]
        print(f"     {p:7s} BTC {int((~bp.won).sum())}/{len(bp):3d}   rest {int((~op.won).sum())}/{len(op):3d}")
