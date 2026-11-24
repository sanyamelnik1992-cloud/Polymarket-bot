"""Maker side of every public taker trade in BTC 5m windows (r22, 18-21 Nov): the maker's outcome PnL per share by how far the
trade price was from our Binance fair value (taker buy above fair = maker sold above fair). Shows what a fair-value maker
quoting at fair +- h would have earned per fill (if at the front of the queue at that price)."""
import json, sys
import numpy as np
from fairlib import p_up, has, mom
D = sys.argv[1]
W = {w["slug"]: w for w in json.load(open(f"{D}/windows.json")) if w["asset"] == "btc" and w["kind"] == "5m"}
TR = json.load(open(f"{D}/trades.json"))
rows = []
for s, w in W.items():
    for ts, sd, out, n, p, wal in TR.get(s, []):
        t = int(ts); rel = t - w["start"]
        if not (0 <= rel < 238) or not (0.03 <= p <= 0.97) or not has("btc", t - 3700): continue
        pu = p_up("btc", w["start"], 300, t, 0)
        pu1 = p_up("btc", w["start"], 300, t, -10)       # 10 s later: markout
        if pu is None or pu1 is None: continue
        f = pu if out == "Up" else 1 - pu; f10 = pu1 if out == "Up" else 1 - pu1
        won = 1.0 if w["win"] == out else 0.0
        # the maker took the other side of the taker: sold the side (taker B) or bought it (taker S)
        g = 1.0 if sd == "S" else -1.0                    # +1: maker long the side
        rows.append((rel, g * (f - p), g * (won - p), g * (f10 - f), n, p,
                     -g * (1 if out == "Up" else -1) * mom("btc", t, 2)))
A = np.array(rows)
rel, edge, pnl, mk10, n, p, pre = A.T
print("maker side of", len(A), "taker trades, shares", round(n.sum()))
print("edge = how much better than our fair the maker traded (c/share); pnl = outcome pnl per share of the maker")
edges = [-1, -0.10, -0.05, -0.03, -0.02, -0.01, 0, 0.01, 0.02, 0.03, 0.05, 0.10, 1]
for lo, hi in zip(edges[:-1], edges[1:]):
    k = (edge >= lo) & (edge < hi)
    if k.sum() < 100: continue
    w_ = n[k]
    print(f"  edge {100*lo:+5.0f}..{100*hi:+4.0f} c: fills {k.sum():7d} shares {w_.sum():9.0f}  outcome pnl {100 * (pnl[k] * w_).sum() / w_.sum():+6.2f} c/share  "
          f"markout 10 s {100 * (mk10[k] * w_).sum() / w_.sum():+6.2f} c")
print("\nmaker fills with edge >= +1 c, by Binance move in the 2 s before toward the maker (bp):")
for lo, hi in ((-99, -2), (-2, -0.5), (-0.5, 0.5), (0.5, 2), (2, 99)):
    k = (edge >= 0.01) & (pre >= lo) & (pre < hi)
    w_ = n[k]
    print(f"  {lo:>4}..{hi:<4}: fills {k.sum():7d} edge {100 * (edge[k] * w_).sum() / w_.sum():+6.2f} c  outcome pnl {100 * (pnl[k] * w_).sum() / w_.sum():+6.2f} c/share")
