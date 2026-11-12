import numpy as np, pandas as pd
d = pd.read_pickle("pop.pkl")
sh = d.reward / (d.reward + d.mrebate.fillna(0)).where((d.reward + d.mrebate.fillna(0)) > 0)
f = d[(d.rpd >= 5) & (sh >= 0.9) & (d.vol > 0)].copy()
f["loss_per_vol"] = -f.pos / f.vol; f["rew_per_vol"] = f.reward / f.vol; f["keep"] = f.net / f.reward
f["fills_pd"] = f.trades / f.days; f["usd_per_fill"] = f.vol / f.trades.where(f.trades > 0)
q = lambda s: np.round(np.nanquantile(s, [.1, .25, .5, .75, .9]), 3)
print("pure farmers (reward >= $5/day, reward share >= 0.9):", len(f), " total reward/day", round(f.rpd.sum()), " total net/day", round(f.net_pd.sum()))
print(" share of reward kept (net/reward) quantiles 10/25/50/75/90:", q(f.keep))
print(" aggregate share kept: %.2f" % (f.net.sum() / f.reward.sum()))
print(" loss per filled $ (quantiles):", q(f.loss_per_vol), " reward per filled $:", q(f.rew_per_vol))
print(" maker fills per day:", q(f.fills_pd), " $ per fill:", q(f.usd_per_fill))
print("\n by breadth (sampled markets where seen as maker, of 600):")
for lo, hi in ((2, 5), (5, 15), (15, 50), (50, 1000)):
    k = f[(f.markets >= lo) & (f.markets < hi)]
    if len(k): print(f"  markets {lo:3d}-{hi:4d}: n={len(k):3d} reward/day med {k.rpd.median():7.1f}  kept med {k.keep.median():5.2f}  agg kept {k.net.sum()/k.reward.sum():5.2f}  net>0 {100*(k.net>0).mean():3.0f}%  $/fill med {k.usd_per_fill.median():6.1f}  fills/day med {k.fills_pd.median():6.1f}  loss per filled $ med {k.loss_per_vol.median():.3f}")
print("\n by average fill size ($ per maker fill):")
for lo, hi in ((0, 5), (5, 15), (15, 50), (50, 200), (200, 1e9)):
    k = f[(f.usd_per_fill >= lo) & (f.usd_per_fill < hi)]
    if len(k): print(f"  ${lo:g}-{hi:g}: n={len(k):3d} reward/day med {k.rpd.median():7.1f}  kept med {k.keep.median():5.2f}  agg kept {k.net.sum()/k.reward.sum():5.2f}  net>0 {100*(k.net>0).mean():3.0f}%  loss per filled $ med {k.loss_per_vol.median():.3f}  rew per filled $ med {k.rew_per_vol.median():.3f}")
print("\n by reward per filled $ (how rarely they are filled):")
for lo, hi in ((0, .02), (.02, .05), (.05, .15), (.15, .5), (.5, 1e9)):
    k = f[(f.rew_per_vol >= lo) & (f.rew_per_vol < hi)]
    if len(k): print(f"  {lo:g}-{hi:g}: n={len(k):3d} reward/day med {k.rpd.median():7.1f}  kept med {k.keep.median():5.2f}  agg kept {k.net.sum()/k.reward.sum():5.2f}  net>0 {100*(k.net>0).mean():3.0f}%  loss per filled $ med {k.loss_per_vol.median():.3f}")
print("\n small pure farmers (reward $5-50/day):")
k = f[(f.rpd < 50)]
print("  n=%d  net>0 %.0f%%  kept med %.2f  net/day quantiles %s  worst day quantiles %s" % (len(k), 100 * (k.net > 0).mean(), k.keep.median(), q(k.net_pd), q(k.worst_day)))
pd.set_option("display.width", 250)
cols = ["w", "markets", "value", "rpd", "pos_pd", "net_pd", "keep", "fills_pd", "usd_per_fill", "loss_per_vol", "rew_per_vol", "worst_day", "reward_days"]
print("\n top pure farmers by reward:")
t = f.sort_values("rpd", ascending=False).head(22).copy(); t["w"] = t.w.str[:10]; print(t[cols].round(2).to_string(index=False))
print("\n small pure farmers ($10-60/day) with the best share kept (>= 20 reward days):")
t = f[(f.rpd >= 10) & (f.rpd < 60) & (f.reward_days >= 20)].sort_values("keep", ascending=False).head(20).copy(); t["w"] = t.w.str[:10]; print(t[cols].round(2).to_string(index=False))
