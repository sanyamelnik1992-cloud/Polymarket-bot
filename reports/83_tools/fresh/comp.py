"""Who takes the lagging asks (5m, edge >= 0.15 by Binance 1 s before, 62-290 s before the end), 18-21 Nov: per wallet the
trades, shares, average price, return at resolution, how often it was the FIRST qualifying trade of its window, how many
seconds after the window's first qualifying trade it came, its typical size. Wallets are printed by prefix only."""
import sys, collections, math, json, statistics as st
sys.path.insert(0, sys.argv[1])
from fv2 import events, fee
ev = [e for e in events("5m", 1) if e["edge"] >= 0.15 and 0.05 <= e["px"] <= 0.95 and e["sd"] == "B" and 62 <= e["x"] <= 290]
ev.sort(key=lambda e: (e["slug"], e["t"]))
first_t = {}
for e in ev: first_t.setdefault(e["slug"], e["t"])
by = collections.defaultdict(list)
for e in ev: by[e["wal"]].append(e)
tot_sh = sum(e["sz"] for e in ev)
rows = []
for w, es in by.items():
    sh = sum(e["sz"] for e in es); cost = sum(e["sz"] * (e["px"] + fee(e["px"])) for e in es)
    pnl = sum(e["sz"] * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) for e in es)
    first = sum(1 for e in es if e["t"] == first_t[e["slug"]])
    delay = st.median(e["t"] - first_t[e["slug"]] for e in es)
    rows.append((sh, w, len(es), cost, pnl, first, delay, st.median(e["sz"] for e in es), len({e["slug"] for e in es}),
                 st.median(e["px"] for e in es), st.median(e["edge"] for e in es)))
rows.sort(reverse=True)
print(f"qualifying trades {len(ev)}, shares {tot_sh:.0f}, wallets {len(by)}; windows with one {len(first_t)}")
print("wallet      trades windows  shares  share%  med_sz  med_px  med_edge  ROI      pnl$    first%  med_s_after_first")
for sh, w, n, cost, pnl, first, delay, msz, nw, mpx, medge in rows[:25]:
    print(f"{w[:10]}  {n:6d} {nw:6d} {sh:8.0f} {100*sh/tot_sh:6.1f}% {msz:7.1f} {mpx:7.3f} {medge:8.3f}  {100*pnl/cost:+6.1f}% {pnl:+8.0f}  {100*first/n:5.1f}%  {delay:6.0f}")
json.dump([r[1] for r in rows[:25]], open("top_wallets.json", "w"))
# speed: who is first most often (windows where they were the first qualifying taker)
fc = collections.Counter(e["wal"] for e in ev if e["t"] == first_t[e["slug"]])
print("first qualifying taker of the window, top 10:", [(w[:10], c) for w, c in fc.most_common(10)], "of", len(first_t))
