import sys
import numpy as np, pandas as pd
o = pd.read_pickle(sys.argv[1])
o["net"] = o.rew - o.loss
pd.set_option("display.width", 250)
print("markets", o.cid.nunique(), " days per half median %.2f" % o.days.median())
print("\n== ALL markets, both halves, per day totals (min size each): by distance and fill rule")
print(f"{'d':>3s} {'rule':>8s} {'capital$':>9s} {'reward/d':>9s} {'fills/d':>8s} {'filled$/d':>9s} {'loss/d':>8s} {'net/d':>8s} {'kept':>5s} {'net %cap/d':>10s} {'loss per filled $':>17s} {'markout 1m/sh':>13s} {'10m/sh':>7s}")
for d in sorted(o.d.unique()):
    for touch in (0, 1):
        k = o[(o.d == d) & (o.touch == touch)]
        g = k.groupby("cid").agg(days=("days", "sum"), rew=("rew", "sum"), loss=("loss", "sum"), fills=("fills", "sum"), usd=("usd", "sum"), S=("S", "first"), sh=("shares", "sum"), l1=("loss1m", "sum"), l10=("loss10m", "sum"))
        cap = g.S.sum(); rpd = (g.rew / g.days).sum(); lpd = (g.loss / g.days).sum(); fpd = (g.fills / g.days).sum(); upd = (g.usd / g.days).sum()
        print(f"{d:3.0f} {'touch' if touch else 'through':>8s} {cap:9.0f} {rpd:9.0f} {fpd:8.0f} {upd:9.0f} {lpd:8.0f} {rpd - lpd:8.0f} {(rpd - lpd) / rpd:5.2f} {100 * (rpd - lpd) / cap:9.2f}% {g.loss.sum() / max(g.usd.sum(), 1):17.3f} {g.l1.sum() / max(g.sh.sum(), 1):13.4f} {g.l10.sum() / max(g.sh.sum(), 1):7.4f}")
# per market table at d=2, through
def per_market(d, touch, half=None):
    k = o[(o.d == d) & (o.touch == touch)]
    if half is not None: k = k[k.half == half]
    g = k.groupby("cid").agg(days=("days", "sum"), rew=("rew", "sum"), loss=("loss", "sum"), fills=("fills", "sum"), usd=("usd", "sum"), S=("S", "first"), rate=("rate", "first"),
                             comp=("comp", "first"), spread=("spread", "first"), vol24=("vol24", "first"), ntr=("ntr", "sum"), rng=("rng", "max"), p=("p", "first"), q=("q", "first"), up=("up", "mean"))
    g["rpd"] = g.rew / g.days; g["lpd"] = g.loss / g.days; g["npd"] = g.rpd - g.lpd; g["fpd"] = g.fills / g.days; g["y"] = g.npd / g.S; g["ry"] = g.rpd / g.S
    return g
g = per_market(2.0, 1)
print("\n== d = 2c, 'touch' rule (pessimistic: more fills). Markets by net per day:")
print(" net > 0: %d of %d markets; net/day quantiles 10/25/50/75/90: %s" % ((g.npd > 0).sum(), len(g), np.round(g.npd.quantile([.1, .25, .5, .75, .9]).values, 2)))
for name, col, bins in (("spread now", "spread", (0, .011, .021, .05, .10, 9)), ("competitiveness", "comp", (-1, 1e-9, 1, 5, 20, 100, 1e9)), ("trades in 7 days", "ntr", (-1, 0.5, 10, 50, 200, 1e9)),
                        ("price range in 7 days", "rng", (-1, .02, .05, .10, .25, 9)), ("pool $/day", "rate", (0, 5, 20, 50, 200, 1e9))):
    print(f"\n by {name}:")
    for lo, hi in zip(bins[:-1], bins[1:]):
        k = g[(g[col] > lo) & (g[col] <= hi)]
        if len(k): print(f"   ({lo:g}, {hi:g}]: n={len(k):4d} capital {k.S.sum():7.0f} reward/d {k.rpd.sum():7.1f} loss/d {k.lpd.sum():7.1f} net/d {k.npd.sum():7.1f} kept {k.npd.sum() / max(k.rpd.sum(), 1e-9):5.2f} net %cap/d {100 * k.npd.sum() / k.S.sum():6.2f}% fills/d per market {k.fpd.mean():5.2f} net>0 {100 * (k.npd > 0).mean():3.0f}%")
g.to_pickle("pm2.pkl")
