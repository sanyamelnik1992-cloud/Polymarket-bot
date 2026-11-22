"""Selective takers = wallets most of whose 5m taker buys sit far below the Binance fair value (our kind of bot). For each wallet
with >= 40 buys: share of buys with edge >= 0.10, ROI of all its buys, buys/window, size, timing, coins, sells."""
import sys, collections, statistics as st
sys.path.insert(0, sys.argv[1])
from fv2 import events, fee
ev = events("5m", 1)
by = collections.defaultdict(list)
for e in ev:
    if e["sd"] == "B": by[e["wal"]].append(e)
sells = collections.Counter(e["wal"] for e in ev if e["sd"] == "S")
rows = []
for w, es in by.items():
    if len(es) < 40: continue
    sel = sum(1 for e in es if e["edge"] >= 0.10) / len(es)
    cost = sum(e["sz"] * (e["px"] + fee(e["px"])) for e in es)
    pnl = sum(e["sz"] * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) for e in es)
    rows.append((sel, w, len(es), pnl, cost))
rows.sort(reverse=True)
print("wallet      buys  edge>=10c  ROI      pnl$   windows  buys/w  med_sz  med_px  med_edge  med_s_before_end  coins  sells")
for sel, w, n, pnl, cost in rows[:20]:
    es = by[w]; pw = collections.Counter(e["slug"] for e in es)
    coins = "".join(sorted({e["a"][0] for e in es}))
    print(f"{w[:10]}  {n:5d}  {100*sel:6.1f}%  {100*pnl/cost:+6.1f}% {pnl:+7.0f}  {len(pw):6d}  {n/len(pw):5.1f}  {st.median(e['sz'] for e in es):6.1f}  "
          f"{st.median(e['px'] for e in es):.3f}  {st.median(e['edge'] for e in es):+.3f}  {st.median(e['x'] for e in es):6.0f}  {coins:5s} {sells[w]}")
print("wallets with >= 40 buys:", len(rows), "; with >= 50 % of buys at edge >= 0.10:", sum(1 for r in rows if r[0] >= 0.5))
