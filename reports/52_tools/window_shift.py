"""Which 60 s does the exchange average? For every window with Gamma's priceToBeat / finalPrice and complete recorded
Chainlink prints: the error of the mean over [T-62+s, T-3+s] (strike) and [T+238+s, T+297+s] (close) for s = -2..2.
7-8 Nov (806 windows): s = 0 fits 537, s = +1 fits 269 exactly; the error of s = 0 reaches 0.27 bp when the price moves.

usage: python reports/52_tools/window_shift.py MARKETS.parquet CHAINLINK.jsonl [CHAINLINK.jsonl ...]
"""
import collections
import json
import sys

import numpy as np
import pandas as pd

SYM = {"btc/usd": "btc", "eth/usd": "eth", "sol/usd": "sol", "xrp/usd": "xrp"}
mk = pd.read_parquet(sys.argv[1]).dropna(subset=["price_to_beat", "final_price"])
cl = collections.defaultdict(dict)
for path in sys.argv[2:]:
    for line in open(path):
        e = json.loads(line)
        a = SYM.get(e["sym"])
        if a:
            cl[a][e["ts"] // 1000] = e["v"]


def mean(a, s0):
    xs = [cl[a].get(s) for s in range(s0, s0 + 60)]
    return None if any(x is None for x in xs) else sum(xs) / 60


rows = []
for r in mk.itertuples():
    a, T = r.asset, int(r.window_start)
    row = {}
    for sh in (-2, -1, 0, 1, 2):
        k, f = mean(a, T - 62 + sh), mean(a, T + 238 + sh)
        if k is None or f is None:
            break
        row[f"k{sh}"] = abs(k / r.price_to_beat - 1) * 1e4
        row[f"f{sh}"] = abs(f / r.final_price - 1) * 1e4
    else:
        row["ours_up"], row["gamma_up"] = mean(a, T + 238) >= mean(a, T - 62), r.winner == "Up"
        rows.append(row)
d = pd.DataFrame(rows)
print("windows with complete prints:", len(d))
for c, name in (("k", "strike"), ("f", "close")):
    best = d[[f"{c}{s}" for s in (-2, -1, 0, 1, 2)]].values.argmin(axis=1) - 2
    q = np.percentile(d[f"{c}0"], [50, 90, 99, 100])
    print(f"{name}: best shift {dict(collections.Counter(best.tolist()))}; error of shift 0, bp: median {q[0]:.4f} "
          f"p90 {q[1]:.4f} p99 {q[2]:.4f} max {q[3]:.4f}")
print("outcome of our rule differs from Gamma:", int((d.ours_up != d.gamma_up).sum()), "of", len(d))
