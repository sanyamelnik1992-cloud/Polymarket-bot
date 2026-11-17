"""Variants of the early 0.99 rule on the per-step table of early_steps.py (books 0.3 s later = the fill)."""
import sys, collections, time
import numpy as np, pandas as pd
d = pd.read_parquet(sys.argv[1]).sort_values(["slug", "t"])
d["rel"] = d.t - d.W
d["day"] = pd.to_datetime(d.W, unit="s").dt.strftime("%m-%d")
def fee(p): return 0.07 * p * (1 - p)
G = [(s, g) for s, g in d.groupby("slug", sort=False)]
print("windows", len(G), "steps", len(d), "| sigma bp/s median: Chainlink", round(d.sig_c.median(), 3), "Binance", round(d.sig_b.median(), 3),
      "| median ratio", round((d.sig_b / d.sig_c).median(), 2))
def run(zcol, z0, lo, hi, pmin, cap, agree=None, sh=10.0):
    ent = []
    for s, g in G:
        g = g[(g.rel >= lo) & (g.rel <= hi)]
        z = g[zcol].values
        ok = np.abs(z) >= z0
        if agree is not None: ok &= (np.abs(g.zc.values) >= agree) & (np.sign(g.zc.values) == np.sign(z))
        for i in np.nonzero(ok)[0]:
            r = g.iloc[i]; up = r[zcol] > 0
            a = r.askU if up else r.askD
            if not (pmin - 1e-9 <= a <= cap + 1e-9): continue
            f, fs, f2, f2s = (r.fU, r.fU_sz, r.fU2, r.fU2_sz) if up else (r.fD, r.fD_sz, r.fD2, r.fD2_sz)
            if not (f <= cap + 1e-9): continue
            got = min(sh, fs); cost = got * f
            if got < sh and f2 == f2 and f2 <= cap + 1e-9:
                q = min(sh - got, f2s); got += q; cost += q * f2
            px = cost / got
            ent.append(dict(slug=s, a=r.a, day=r.day, rel=r.rel, z=abs(r[zcol]), px=px, sh=got, won=(r.win == 1) == up))
            break
    return ent
def show(name, ent):
    n = len(ent)
    if not n: print(f"{name:44} no entries"); return
    lost = sum(not e["won"] for e in ent); shs = sum(e["sh"] for e in ent)
    pnl = sum(e["sh"] * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) for e in ent)
    by = collections.defaultdict(lambda: [0, 0])
    for e in ent: by[e["day"]][0] += 1; by[e["day"]][1] += (not e["won"])
    print(f"{name:44} n {n:4} lost {lost:3} ({100*lost/n:4.2f}%) px {np.mean([e['px'] for e in ent]):.4f} pnl/sh {100*pnl/shs:+.2f}c $@10sh {pnl:+7.2f} | " + " ".join(f"{k}:{v[0]}/{v[1]}" for k, v in sorted(by.items())))
for zcol, ag in (("z", 2.5), ("zb", None)):
    for z0 in (3.0, 3.5, 4.0, 5.0):
        show(f"{zcol} z>={z0} rel180-238 0.985-0.99", run(zcol, z0, 180, 238, 0.985, 0.99, ag if ag is None else min(ag, z0 - 0.5)))
    show(f"{zcol} z>=3 rel120-238 0.985-0.99", run(zcol, 3.0, 120, 238, 0.985, 0.99, ag if ag is None else 2.5))
    show(f"{zcol} z>=3 rel180-238 0.95-0.984 (cheap)", run(zcol, 3.0, 180, 238, 0.95, 0.984, ag if ag is None else 2.5))
# calibration: first moment in rel 180-238 with |z| >= 2, outcome by z bucket, whatever the ask
for zcol in ("z", "zb"):
    rows = []
    for s, g in G:
        g = g[(g.rel >= 180) & (g.rel <= 238)]
        if not len(g): continue
        r = g.iloc[len(g) // 2]          # the middle of the zone (about 91 s before the end)
        up = r[zcol] > 0; a = r.askU if up else r.askD
        rows.append((abs(r[zcol]), (r.win == 1) == up, a))
    c = pd.DataFrame(rows, columns=["z", "won", "ask"])
    c["b"] = pd.cut(c.z, [0, 1, 2, 2.5, 3, 3.5, 4, 5, 7, 100])
    t = c.groupby("b", observed=True).agg(n=("won", "size"), lost=("won", lambda x: int((~x).sum())), ask_med=("ask", "median"),
                                         ask_le099=("ask", lambda x: float((x <= 0.99).mean())))
    t["lost%"] = (100 * t.lost / t.n).round(2)
    print(f"\ncalibration by {zcol} at the middle of the zone (one row per window):\n{t}")
