"""The top systematic takers of lagging asks: all their taker trades in the 5m windows (not only the qualifying ones):
edge distribution, sells, entries per window, timing in the window, coins, the size they take, net shares per window."""
import sys, collections, json, statistics as st
sys.path.insert(0, sys.argv[1])
from fv2 import events, fee
ev = events("5m", 1)
TOP = ["0xeebde7a0", "0xc53375ff", "0x19b38230", "0x0ca4e9d2", "0x129246be", "0x20d2309c", "0xb9587c37", "0xd84403ef"]
for pre in TOP:
    es = [e for e in ev if e["wal"].startswith(pre)]
    if not es: continue
    buys = [e for e in es if e["sd"] == "B"]; sells = [e for e in es if e["sd"] == "S"]
    q = [e for e in buys if e["edge"] >= 0.15]
    eb = collections.Counter(min(4, max(-1, int(e["edge"] // 0.05))) for e in buys)
    per_w = collections.Counter(e["slug"] for e in buys)
    xs = [e["x"] for e in buys]
    coins = collections.Counter(e["a"] for e in buys)
    sides = collections.defaultdict(set)
    for e in buys: sides[e["slug"]].add(e["side"])
    both = sum(1 for s in sides.values() if len(s) == 2)
    pnl = sum(e["sz"] * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) for e in buys)
    cost = sum(e["sz"] * (e["px"] + fee(e["px"])) for e in buys)
    print(f"{pre}: taker buys {len(buys)} (edge>=0.15: {len(q)}), taker sells {len(sells)}; windows {len(per_w)}, buys/window median "
          f"{st.median(per_w.values())}, max {max(per_w.values())}; both sides in {both} windows; ROI all buys {100*pnl/cost:+.1f}% (${pnl:+.0f})")
    print(f"   edge of its buys (5c bins from <-0.05): {dict(sorted(eb.items()))}; s before end: median {st.median(xs):.0f}, "
          f"10-90% {sorted(xs)[len(xs)//10]:.0f}-{sorted(xs)[9*len(xs)//10]:.0f}; coins {dict(coins)}; size median {st.median(e['sz'] for e in buys):.0f}")
