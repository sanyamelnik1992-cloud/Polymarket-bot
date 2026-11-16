"""Weather endgame without any model: late in the local day, once the temperature has fallen d degrees below the day's
running max, how often is the bucket of the running max the winner, and at what prices did people buy it then?"""
import json, sys, bisect, collections
sys.path.insert(0, ".")
from wxlib import qualifying
days = json.load(open("days.json")); trades = json.load(open("trades.json"))
LAG, FEE = 300, 0.05
def state_at(day, t):
    obs = day["obs"]; i = bisect.bisect_right([o[0] + LAG for o in obs], t)
    if i == 0: return None
    seen = obs[:i]; M = max(o[2] for o in seen)
    return seen[-1][1], M, seen[-1][2]
print("1) how often the running-max bucket wins, by local hour and drop below the max (one sample per station-day and hour):")
grid = collections.defaultdict(lambda: [0, 0])
for slug, day in days.items():
    qF = qualifying(day["F"], day["unit"]); run = -1e9; seen_h = set()
    for ts, hr, t in day["obs"]:
        run = max(run, t); h = int(hr)
        if h in seen_h: continue
        seen_h.add(h)
        d = run - t
        db = "d<1" if d < 1 else "d 1-2" if d < 2 else "d 2-3" if d < 3 else "d>=3"
        g = grid[(h, db)]; g[0] += 1; g[1] += qualifying(run, day["unit"]) == qF
for h in range(12, 24):
    print(f"  {h:02d}h " + "  ".join(f"{db}: {100 * grid[(h, db)][1] / grid[(h, db)][0]:5.1f}% ({grid[(h, db)][0]:3d})" if grid[(h, db)][0] else f"{db}:    -      "
                                    for db in ("d<1", "d 1-2", "d 2-3", "d>=3")))
print("\n2) taker buys of the running-max bucket (YES) late in the day, and realized result per share (fee 0.05 p(1-p)):")
res = collections.defaultdict(list)
for slug, day in days.items():
    qF = qualifying(day["F"], day["unit"])
    for b in day["buckets"]:
        for tr in trades.get(b["cid"], []):
            if tr["side"] != "BUY": continue
            st = state_at(day, tr["t"])
            if not st: continue
            hr, M, T = st
            if not (b["lo"] <= qualifying(M, day["unit"]) <= b["hi"]): continue      # the bucket of the running max
            yes = tr["o"] == "Yes"
            if not yes: continue
            h = int(hr); d = M - T
            px = tr["px"]; pnl = b["won"] - px - FEE * px * (1 - px)
            for key in ((h >= 16, d >= 1), (h >= 17, d >= 2), (h >= 18, d >= 2), (h >= 20, d >= 1)):
                pass
            for lab, ok in (("h>=16, d>=1", h >= 16 and d >= 1), ("h>=17, d>=2", h >= 17 and d >= 2), ("h>=18, d>=2", h >= 18 and d >= 2),
                            ("h>=19, d>=3", h >= 19 and d >= 3), ("h>=21", h >= 21)):
                if ok: res[lab].append((px, b["won"], pnl, slug, tr["sz"]))
for lab, xs in res.items():
    n = len(xs); ev_ = len({x[3] for x in xs}); lost = sum(not x[1] for x in xs)
    pb = collections.defaultdict(list)
    for x in xs: pb["<.9" if x[0] < .9 else ".90-.95" if x[0] < .95 else ".95-.98" if x[0] < .98 else ">=.98"].append(x)
    print(f"  {lab:12s} buys {n:6d} events {ev_:4d} lost {lost:5d} ({100 * lost / n:4.1f}%) avg {100 * sum(x[2] for x in xs) / n:+6.2f}c | " +
          " | ".join(f"{k}: n {len(v)} lost {sum(not x[1] for x in v)} {100 * sum(x[2] for x in v) / len(v):+.2f}c" for k, v in sorted(pb.items())))
