"""Trend features for the state grids of reports/52_tools/grid.py: how far the price moved AGAINST the favoured side
over the last N seconds (bp, positive = toward the strike), from Binance mids (tr_bnN) and Chainlink prints (tr_clN).
Writes GRID_DIR/feat/PERIOD.parquet with the same row order as GRID_DIR/PERIOD.parquet.

usage: python reports/59_tools/feat.py DATA_DIR GRID_DIR PERIOD [PERIOD ...]"""
import json, os, sys
import numpy as np, pandas as pd
BN = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
NS = (5, 10, 15, 20, 30, 45, 60)
def jl(path):
    with open(path) as f:
        for line in f:
            try: yield json.loads(line)
            except ValueError: pass
data, grid = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(grid, "feat"), exist_ok=True)
for per in sys.argv[3:]:
    g = pd.read_parquet(os.path.join(grid, per + ".parquet"))
    bn = pd.DataFrame(jl(os.path.join(data, per + "_binance.jsonl"))); bn["mid"] = (bn.bid + bn.ask) / 2
    cl = pd.DataFrame(jl(os.path.join(data, per + "_chainlink.jsonl")))
    out = pd.DataFrame(index=g.index)
    for n in NS: out[f"tr_bn{n}"] = np.nan; out[f"tr_cl{n}"] = np.nan
    for a in g.asset.unique():
        ia = (g.asset == a).values
        t = g.t.values[ia]; sg = np.where(g.side.values[ia] == "U", 1.0, -1.0)
        b = bn[bn.sym == BN[a]].sort_values("rx"); rx, mid = b.rx.values, b.mid.values
        c = cl[cl.sym == a + "/usd"].sort_values("rx"); crx, cv = c.rx.values, c.v.values
        def at(x, xs, vs):
            i = np.searchsorted(xs, x, side="right") - 1
            return np.where(i >= 0, vs[np.maximum(i, 0)], np.nan)
        b0, c0 = at(t, rx, mid), at(t, crx, cv)
        for n in NS:
            out.loc[ia, f"tr_bn{n}"] = -sg * np.log(b0 / at(t - n * 1000, rx, mid)) * 1e4
            out.loc[ia, f"tr_cl{n}"] = -sg * np.log(c0 / at(t - n * 1000, crx, cv)) * 1e4
    out.to_parquet(os.path.join(grid, "feat", per + ".parquet")); print(per, len(out), flush=True)
