"""Do liquidity-reward earners make money? Public monthly PnL series of maker wallets seen in rewarded markets.
usage: an_pop.py PNL.jsonl [MIN_DAYS=10]"""
import json, sys
import numpy as np, pandas as pd
K = ("timestamp", "reward_income", "maker_rebate", "taker_rebate", "yield_income", "position_pnl", "realized_pnl", "unrealized_pnl",
     "trade_pnl", "economic_pnl", "volume_usdc", "trade_count", "fees")
MIN_DAYS = float(sys.argv[2]) if len(sys.argv) > 2 else 10
rows = []
for line in open(sys.argv[1]):
    r = json.loads(line)
    pts = r["pts"]
    if len(pts) < 2: continue
    a, b = dict(zip(K, pts[0])), dict(zip(K, pts[-1]))
    days = (b["timestamp"] - a["timestamp"]) / 86400
    if days < MIN_DAYS: continue
    def dd(k):
        return np.nan if a.get(k) is None or b.get(k) is None else b[k] - a[k]
    daily = pd.DataFrame(pts, columns=K)
    de = daily.economic_pnl.astype(float).diff().dropna()
    dr = daily.reward_income.astype(float).diff().dropna()
    rows.append(dict(w=r["w"], markets=r["markets"], fills=r["fills"], value=r.get("value") or 0.0, days=days, reward=dd("reward_income"),
                     mrebate=dd("maker_rebate"), trebate=dd("taker_rebate"), pos=dd("position_pnl"), econ=dd("economic_pnl"), vol=dd("volume_usdc"),
                     trades=dd("trade_count"), fees=dd("fees"), worst_day=de.min() if len(de) else np.nan, reward_days=int((dr > 0).sum()),
                     reward_life=b.get("reward_income") or 0.0))
d = pd.DataFrame(rows)
d["rpd"] = d.reward / d.days; d["net"] = d.reward + d.mrebate.fillna(0) + d.trebate.fillna(0) + d.pos
d["net_pd"] = d.net / d.days; d["pos_pd"] = d.pos / d.days; d["vol_pd"] = d.vol / d.days
d["r_per_vol_bp"] = 1e4 * d.reward / d.vol.where(d.vol > 0)
d.to_pickle("pop.pkl")
print("wallets with >= %.0f days of history: %d; with rewards in the window: %d; reward >= $1/day: %d" % (MIN_DAYS, len(d), int((d.reward > 0).sum()), int((d.rpd >= 1).sum())))
tot = d[d.reward > 0]
print("ALL reward earners in the sample: reward $%.0f/day, maker rebates $%.0f/day, position PnL $%.0f/day, net $%.0f/day" % (tot.rpd.sum(), (tot.mrebate / tot.days).sum(), tot.pos_pd.sum(), tot.net_pd.sum()))
def block(name, k):
    if not len(k): print(f"{name:34s} n=0"); return
    q = lambda s, p: np.nanquantile(s, p)
    print(f"{name:34s} n={len(k):4d}  reward/day med {k.rpd.median():8.1f} sum {k.rpd.sum():8.0f} | pos PnL/day med {k.pos_pd.median():8.1f} sum {k.pos_pd.sum():9.0f} | "
          f"net>0: {100*(k.net > 0).mean():4.0f}%  pos>0: {100*(k.pos > 0).mean():4.0f}%  lost more on positions than rewards: {100*(-k.pos > k.reward).mean():4.0f}% | "
          f"net/day q25 {q(k.net_pd, .25):8.1f} med {k.net_pd.median():7.1f} q75 {q(k.net_pd, .75):8.1f} | value med {k.value.median():8.0f}")
print("\n== by reward per day")
for lo, hi in ((1, 5), (5, 20), (20, 100), (100, 500), (500, 1e9)):
    block(f"reward ${lo:g}-{hi:g}/day", d[(d.rpd >= lo) & (d.rpd < hi)])
print("\n== reward earners (>= $5/day) by reward per $ of FILLED volume (high = rarely filled, 'pure farmer')")
f = d[d.rpd >= 5]
for lo, hi in ((0, 2), (2, 10), (10, 50), (50, 200), (200, 1e9)):
    block(f"reward/volume {lo:g}-{hi:g} bp", f[(f.r_per_vol_bp >= lo) & (f.r_per_vol_bp < hi)])
print("\n== reward earners (>= $5/day) by reward share of income (reward / (reward + maker rebates))")
sh = f.reward / (f.reward + f.mrebate.fillna(0)).where((f.reward + f.mrebate.fillna(0)) > 0)
for lo, hi in ((0, .2), (.2, .5), (.5, .9), (.9, 1.01)):
    block(f"reward share {lo:g}-{hi:g}", f[(sh >= lo) & (sh < hi)])
print("\n== reward earners (>= $5/day) by portfolio value")
for lo, hi in ((0, 500), (500, 2000), (2000, 10000), (10000, 50000), (50000, 1e12)):
    k = f[(f.value >= lo) & (f.value < hi)]
    block(f"value ${lo:g}-{hi:g}", k)
    if len(k): print(f"{'':34s} reward per $ of position value per day: median {100*(k.rpd / k.value.where(k.value > 0)).median():.2f}%  net per $ per day median {100*(k.net_pd / k.value.where(k.value > 0)).median():.2f}%")
pd.set_option("display.width", 260)
cols = ["w", "markets", "value", "days", "rpd", "mrebate", "pos_pd", "net_pd", "vol_pd", "r_per_vol_bp", "worst_day"]
print("\n== top 25 by net/day among reward earners with reward share >= 0.5")
k = f[sh >= 0.5].sort_values("net_pd", ascending=False).head(25).copy(); k["w"] = k.w.str[:10]; k["mrebate"] = k.mrebate / k.days
print(k[cols].round(1).to_string(index=False))
print("\n== bottom 15")
k = f[sh >= 0.5].sort_values("net_pd").head(15).copy(); k["w"] = k.w.str[:10]; k["mrebate"] = k.mrebate / k.days
print(k[cols].round(1).to_string(index=False))
