"""Visits of makers to pools nobody scored in (poll.py output): is a scoring two-sided quote there, for how long.
usage: poll_an.py POLL.jsonl SNAP.json"""
import json, sys, collections
import numpy as np
snap = json.load(open(sys.argv[2]))["rows"]
polls = [json.loads(l) for l in open(sys.argv[1])]
print("polls", len(polls), " span %.0f min, step median %.0f s" % ((polls[-1]["t"] - polls[0]["t"]) / 60, np.median(np.diff([p["t"] for p in polls]))))
cids = sorted({c for p in polls for c in p["b"]})
def scoring(c, rec):
    if rec is None: return None
    pb, pa = rec[0], rec[1]
    if pb is None or pa is None: return False
    return (pa - pb) * 100 < 2 * snap[c]["v"] - 1e-9          # both best min-size levels are inside the zone around their mid
state = {c: [scoring(c, p["b"].get(c)) for p in polls] for c in cids}
frac = {c: np.mean([x for x in s if x is not None]) for c, s in state.items() if any(x is not None for x in s)}
f = np.array(list(frac.values()))
print("markets", len(frac), " never a scoring quote: %d (%.0f%%);  always: %d;  part of the time: %d" % ((f == 0).sum(), 100 * (f == 0).mean(), (f == 1).sum(), ((f > 0) & (f < 1)).sum()))
pool = lambda cs: sum(snap[c]["rate"] for c in cs)
never = [c for c, x in frac.items() if x == 0]; some = [c for c, x in frac.items() if 0 < x < 1]; always = [c for c, x in frac.items() if x == 1]
print("pools: never $%.0f/day, part of the time $%.0f/day, always $%.0f/day" % (pool(never), pool(some), pool(always)))
# visits: runs of consecutive scoring polls
dur = []; n_vis = collections.Counter()
step = float(np.median(np.diff([p["t"] for p in polls])))
for c, s in state.items():
    run = 0; started_inside = s[0] is True
    for i, x in enumerate(s):
        if x: run += 1
        else:
            if run and not (started_inside and i == run): dur.append(run * step); n_vis[c] += 1
            run = 0
dur = np.array(dur)
if len(dur):
    print("visits that began and ended inside the span: %d in %d markets; duration quantiles 10/25/50/75/90 (s): %s; shorter than 3 min: %.0f%%"
          % (len(dur), len(n_vis), np.round(np.quantile(dur, [.1, .25, .5, .75, .9])), 100 * (dur < 180).mean()))
arrive = sum(1 for c, s in state.items() if s[0] is False and any(x for x in s))
print("markets empty at the first poll where a scoring quote appeared later: %d of %d (%.0f%%) in %.0f min"
      % (arrive, sum(1 for s in state.values() if s[0] is False), 100 * arrive / max(1, sum(1 for s in state.values() if s[0] is False)), (polls[-1]["t"] - polls[0]["t"]) / 60))
