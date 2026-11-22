"""The bot's own limits on the replay: one 5m cycle = 4 coins; at most MAX_OPEN entries open at once (all windows of the cycle),
at most N entries per window, GAP s between entries of a window, a fill with probability FILL (our paper fills ~45 % of the
orders: the rest of the asks are gone). 5 shares a day at the paid price."""
import sys, collections, math, random, time
sys.path.insert(0, sys.argv[1])
from fv2 import events, fee
import numpy as np
ev = [e for e in events("5m", 1) if e["edge"] >= 0.15 and 0.05 <= e["px"] <= 0.95 and e["sd"] == "B" and 62 <= e["x"] <= 290]
ev.sort(key=lambda e: e["t"])
def sim(N, GAP, MAX_OPEN=4, FILL=0.45, seed=1, sh=5):
    rnd = random.Random(seed)
    per_w, last, cyc_open = collections.Counter(), {}, collections.Counter()
    pnl = collections.defaultdict(float); n = 0; cost = 0.0; cl = collections.defaultdict(float)
    tried = set()
    for e in ev:
        s = e["slug"]; cyc = int(s.rsplit("-", 1)[1])           # all coins of one 5m window close together: one cycle
        if per_w[s] >= N or (s in last and e["t"] - last[s] < GAP) or cyc_open[cyc] >= MAX_OPEN: continue
        if rnd.random() > FILL: continue                         # the ask was gone when our order arrived
        per_w[s] += 1; last[s] = e["t"]; cyc_open[cyc] += 1; n += 1
        v = ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) * sh
        pnl[time.strftime("%d", time.gmtime(e["t"]))] += v; cost += (e["px"] + fee(e["px"])) * sh; cl[s] += v
    days = len(pnl); tot = sum(pnl.values()); xs = np.array(list(cl.values()))
    worst = min(pnl.values())
    return n / days, tot / days, 100 * tot / cost, worst, xs.mean() / xs.std(ddof=1) * math.sqrt(len(xs)), dict(sorted(pnl.items()))
for FILL in (1.0, 0.45):
    print(f"--- fill probability {FILL}")
    for N, GAP in ((1, 0), (2, 5), (3, 5), (3, 15), (2, 0), (3, 0)):
        r = [sim(N, GAP, FILL=FILL, seed=k) for k in range(5)]
        a = np.mean([x[0] for x in r]); b = np.mean([x[1] for x in r]); c = np.mean([x[2] for x in r]); t = np.mean([x[4] for x in r])
        print(f"N {N} gap {GAP:2d}s: {a:5.0f} entries/day, ${b:+6.1f}/day at 5 sh, ROI {c:+5.1f}%, t {t:+5.1f}, days {r[0][5]}")
