"""Realized edge of weather specialists (weather_spec.json) in resolved weather markets: role, price, hours before the end.
usage: python wx_edge.py   (needs trades_weather.json, wx_markets.json, weather_spec.json, upnl.json)"""
import json, collections, sys, time, calendar
from datetime import datetime
sys.path.insert(0, ".")
from catlib import cat
tr = json.load(open("trades_weather.json")); mk = json.load(open("wx_markets.json")); spec = set(json.load(open("weather_spec.json")))
u = json.load(open("upnl.json"))
def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
agg = collections.defaultdict(lambda: [0.0, 0.0, 0])
perw = collections.defaultdict(lambda: [0.0, 0.0, 0, 0])
for w, rows in tr.items():
    if w not in spec: continue
    for r in rows:
        m = mk.get(r["s"])
        if not m or r["side"] != "BUY" or m["yes"] is None: continue
        won = (m["yes"] == 1.0) == (r["o"] == "Yes")
        lead = (ts(m["end"]) - r["t"]) / 3600
        px, sh = r["px"], r["sz"]
        g = (won - px) * sh
        pb = "<.05" if px < .05 else ".05-.2" if px < .2 else ".2-.5" if px < .5 else ".5-.8" if px < .8 else ".8-.95" if px < .95 else ">=.95"
        lb = "<6h" if lead < 6 else "6-12h" if lead < 12 else "12-24h" if lead < 24 else "24-48h" if lead < 48 else ">48h"
        role = "taker" if r["tk"] else "maker"
        for k in ((role, "price", pb), (role, "lead", lb), (role, "all", ""), ("both", "all", "")):
            a = agg[k]; a[0] += sh; a[1] += g; a[2] += 1
        p = perw[w]; p[0] += sh; p[1] += g; p[2] += 1; p[3] += px * sh
print("weather specialists, buys in resolved weather markets: edge = payoff - price (c a share), before fees (weather has ~no taker fee?)")
for k in sorted(agg):
    s, g, n = agg[k]
    print(f"  {k[0]:5s} {k[1]:5s} {k[2]:7s} buys {n:6d} shares {s:9.0f} edge {100 * g / s:+6.2f}c  $ {g:+9.0f}")
print("\nper wallet (>=50 resolved buys): edge c/share, $, and 30-day economic PnL from the exchange")
now = time.time()
for w, p in sorted(perw.items(), key=lambda kv: -kv[1][1]):
    if p[2] < 50: continue
    pts = u.get(w) or []
    base = [x for x in pts if x["timestamp"] < now - 30 * 86400]
    pnl30 = (pts[-1]["economic_pnl"] - (base[-1]["economic_pnl"] if base else 0)) if pts else None
    print(f"  {w[:8]} buys {p[2]:5d} avg px {p[3] / p[0]:.2f} edge {100 * p[1] / p[0]:+6.2f}c  ${p[1]:+8.0f}  pnl30 {pnl30 if pnl30 is None else round(pnl30)}")
