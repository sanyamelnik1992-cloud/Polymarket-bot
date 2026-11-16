"""Endgame weather backtest, observations only (no forecast): at the moment of every real taker trade, the model's
probability of the bucket from the running max of the day and an empirical table of how much the max still rises
(by local hour and by how far the temperature already fell from the max), estimated WITHOUT the trade's own date.
A simulated buy happens when model - price - fee >= margin, at that trade's price (someone did buy there)."""
import json, sys, bisect, collections, math
sys.path.insert(0, ".")
from wxlib import qualifying
days = json.load(open("days.json")); trades = json.load(open("trades.json"))
FEE = 0.05
LAG = 300                      # an observation is usable 5 minutes after its time stamp (publication delay)
def dbin(d): return 0 if d <= 0.05 else 1 if d <= 1.05 else 2 if d <= 2.05 else 3 if d <= 4.05 else 4
def state(day, t):
    obs = day["obs"]; i = bisect.bisect_right([o[0] + LAG for o in obs], t)
    if i == 0: return None
    seen = obs[:i]; M = max(o[2] for o in seen); T = seen[-1][2]; h = int(seen[-1][1])
    return h, M, T
# residual samples: for every day and every observation time, x = F - running max
samples = collections.defaultdict(list)          # (date, h, dbin) -> [x]
for slug, day in days.items():
    run = -1e9
    for ts, hr, t in day["obs"]:
        run = max(run, t)
        samples[(day["date"], int(hr), dbin(run - t))].append(day["F"] - run)
dates = sorted({d["date"] for d in days.values()})
def table_without(date):
    tab = collections.defaultdict(list); tab_h = collections.defaultdict(list)
    for (d, h, b), xs in samples.items():
        if d == date: continue
        tab[(h, b)] += xs; tab_h[h] += xs
    return tab, tab_h
TABLES = {d: table_without(d) for d in dates}
def prob(day, bucket, h, M, T):
    tab, tab_h = TABLES[day["date"]]
    xs = tab.get((h, dbin(M - T))) or []
    if len(xs) < 30: xs = tab_h.get(h) or []
    if len(xs) < 30: return None
    hit = sum(1 for x in xs if bucket["lo"] <= qualifying(M + x, day["unit"]) <= bucket["hi"])
    return hit / len(xs)
rows = []
for slug, day in days.items():
    for b in day["buckets"]:
        for tr in trades.get(b["cid"], []):
            if tr["side"] != "BUY": continue
            st = state(day, tr["t"])
            if not st: continue
            h, M, T = st
            p = prob(day, b, h, M, T)
            if p is None: continue
            yes = tr["o"] == "Yes"
            pm = p if yes else 1 - p
            win = b["won"] if yes else not b["won"]
            px = tr["px"]
            rows.append(dict(slug=slug, h=h, px=px, pm=pm, win=win, sz=tr["sz"], yes=yes, edge=pm - px - FEE * px * (1 - px),
                             pnl=(win - px - FEE * px * (1 - px)), date=day["date"]))
json.dump(rows, open("bt_rows.json", "w"))
print("taker buys with a same-day state:", len(rows), "events", len({r['slug'] for r in rows}))
# calibration of the model on all these states (trade-weighted)
cal = collections.defaultdict(lambda: [0, 0])
for r in rows:
    k = min(int(r["pm"] * 10), 9); cal[k][0] += 1; cal[k][1] += r["win"]
print("calibration (model prob bin -> share that won):", {f"{k / 10:.1f}": f"{v[1] / v[0]:.3f} ({v[0]})" for k, v in sorted(cal.items())})
def sim(margin, hmin=0, pmin=0.0, pxmax=1.0, label=""):
    sel = [r for r in rows if r["edge"] >= margin and r["h"] >= hmin and r["pm"] >= pmin and r["px"] <= pxmax]
    if not sel: return
    ev_n = len({(r["slug"]) for r in sel})
    per_sh = sum(r["pnl"] for r in sel) / len(sel)
    lost = sum(not r["win"] for r in sel)
    days_ = collections.defaultdict(float)
    for r in sel: days_[r["date"]] += r["pnl"]
    pos = sum(v > 0 for v in days_.values())
    print(f"  {label:34s} buys {len(sel):6d} events {ev_n:4d} lost {lost:5d} ({100 * lost / len(sel):4.1f} %) avg price {sum(r['px'] for r in sel) / len(sel):.3f}"
          f" realized {100 * per_sh:+6.2f}c/share (model said {100 * sum(r['edge'] for r in sel) / len(sel):+5.2f}c)  days + {pos}/{len(days_)}")
print("\nsimulated buys (1 share per qualifying real taker buy, at its price):")
for m in (0.02, 0.05, 0.10, 0.20):
    sim(m, label=f"margin {m:.2f}")
for hmin in (12, 15, 17, 19):
    sim(0.05, hmin=hmin, label=f"margin 0.05, local hour >= {hmin}")
for pmin in (0.9, 0.97, 0.99):
    sim(0.03, pmin=pmin, label=f"margin 0.03, model >= {pmin}")
sim(0.03, pmin=0.97, pxmax=0.97, label="margin 0.03, model >= .97, px <= .97")
