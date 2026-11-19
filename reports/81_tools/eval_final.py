"""The paper configuration of pmbot-lock-early on a per-step book table: Chainlink z >= 3.5 and received-prints z >= 3 on
the same side, window seconds 120-238, asks 0.985-0.99 (fill = book 0.3 s later), BTC / SOL / XRP; plus all coins."""
import sys, collections
import numpy as np, pandas as pd
fee = lambda p: 0.07 * p * (1 - p)
for path in sys.argv[1:]:
    d = pd.read_parquet(path).sort_values(["slug", "t"]); d["rel"] = d.t - d.W
    d["day"] = pd.to_datetime(d.W, unit="s").dt.strftime("%m-%d")
    for coins in (("btc", "sol", "xrp"), ("btc", "eth", "sol", "xrp")):
        ent = []
        for s, g in d[d.a.isin(coins)].groupby("slug", sort=False):
            g = g[(g.rel >= 120) & (g.rel <= 238)]
            ok = (g.z.abs() >= 3.5) & (g.zc.abs() >= 3.0) & (np.sign(g.z) == np.sign(g.zc))
            for i in np.nonzero(ok.values)[0]:
                r = g.iloc[i]; up = r.z > 0
                a = r.askU if up else r.askD
                if not (0.985 - 1e-9 <= a <= 0.99 + 1e-9): continue
                f = r.fU if up else r.fD
                if not (f <= 0.99 + 1e-9): continue
                ent.append(dict(a=r.a, day=r.day, px=f, won=(r.win == 1) == up)); break
        n = len(ent); lost = sum(not e["won"] for e in ent); pnl = sum((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"]) for e in ent)
        by = collections.defaultdict(lambda: [0, 0])
        for e in ent: by[e["day"]][0] += 1; by[e["day"]][1] += not e["won"]
        print(f"{path.split('/')[-1]} {'+'.join(coins):15} windows {d[d.a.isin(coins)].slug.nunique()} entries {n} lost {lost} ({100*lost/max(n,1):.2f}%) "
              f"c/share {100*pnl/max(n,1):+.2f} $@5sh {5*pnl:+.2f} | " + " ".join(f"{k}:{v[0]}/{v[1]}" for k, v in sorted(by.items())))
