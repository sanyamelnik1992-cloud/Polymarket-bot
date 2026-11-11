"""Results of cands15.py: entries of the 15m rule by tier, period and coin, with the live limits (one entry at a time, 5 shares),
P&L with the taker fee. usage: python reports/65_tools/an15.py CANDS.json"""
import json
import sys

import pandas as pd

sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "..", ".."))
from pmbot.fees import fee_per_share  # noqa: E402

d = json.load(open(sys.argv[1]))
e = pd.DataFrame(d["entries"]).drop_duplicates(["a", "ws"]).sort_values(["ws", "s"]).reset_index(drop=True)
e["pnl"] = 5 * (e.won.astype(float) - e.px - e.px.map(fee_per_share))
e["tier"] = pd.cut(e.p, [0.5, 0.95, 0.97, 0.99, 1.01], labels=["4 .90-.95", "3 .95-.97", "2 .97-.99", "1 >=.99"], right=False)
e["end"] = e.ws + 900
# one entry at a time: an entry blocks the others until its window ends (all 15m windows end together)
keep, busy_until = [], 0
for r in e.sort_values(["ws", "s", "p"], ascending=[True, True, False]).itertuples():
    if r.ws in {k[0] for k in keep}:
        continue
    keep.append((r.ws, r.Index))
x = e.loc[[i for _, i in keep]]
days = (e.ws.max() - e.ws.min() + 900) / 86400
print(f"15m windows with a tape: {d['windows']}, days {days:.2f}; entries (one per window per coin): {len(e)}, one at a time: {len(x)}")
for name, df in (("all coins, every window", e), ("one entry at a time (live limits)", x)):
    L = int((~df.won).sum())
    print(f"== {name}: n {len(df)}, lost {L} ({100 * L / max(len(df), 1):.1f}%), pnl {df.pnl.sum():+.2f}, per entry {df.pnl.mean():+.3f}, per day {df.pnl.sum() / days:+.2f}")
    for col in ("tier", "a", "period"):
        g = df.groupby(col, observed=True).agg(n=("pnl", "size"), lost=("won", lambda w: int((~w).sum())), pnl=("pnl", "sum"))
        print("   " + " | ".join(f"{k}: {r.n}/{r.lost}/{r.pnl:+.2f}" for k, r in g.iterrows()))
print("losses:")
print(e[~e.won][["period", "a", "ws", "s", "side", "p", "px", "pnl"]].to_string())
