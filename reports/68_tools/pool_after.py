"""What happens to a pool nobody scored in, hours later: who came, what the pool became, what a minimum-size quote
earns there then. usage: pool_after.py ACTIVE.json (the start, scale.py) CANDS.pkl LATER_SNAP.json [LATER_SNAP.json ...]"""
import json, sys
import numpy as np, pandas as pd
m0 = {m["condition_id"]: dict(rate=m["_rate"], comp=float(m.get("market_competitiveness") or 0)) for m in json.load(open(sys.argv[1]))}
k = pd.read_pickle(sys.argv[2]).set_index("cid")
def has(r): return r is not None and r["comp"] * r["rate"] >= 1.0          # at least one point of total score
base = [c for c, r in m0.items() if c in k.index and r["rate"] >= 20 and not has(r)]
print("empty at the start with a pool >= $20 (candidates):", len(base), "pools $%.0f/day" % sum(m0[c]["rate"] for c in base))
ours = 20 * (2.5 / 4.5) ** 2                                               # 20 shares 2c from the mid in a 4.5c zone
for f in sys.argv[3:]:
    d = json.load(open(f)); s = d["rows"]
    gone = [c for c in base if c not in s]; came = [c for c in base if c in s and has(s[c])]; stay = [c for c in base if c in s and not has(s[c])]
    print(f"\n{f}: no longer rewarded {len(gone)}; somebody scores {len(came)} ({100*len(came)/len(base):.0f}%); still empty {len(stay)} ({100*len(stay)/len(base):.0f}%)")
    for lab, cs in (("somebody came", came), ("still empty", stay)):
        if not cs: continue
        r = np.array([s[c]["rate"] / m0[c]["rate"] for c in cs])
        print(f"   {lab:14s}: pool now / pool at the start, quantiles 10/25/50/75/90 {np.round(np.quantile(r, [.1,.25,.5,.75,.9]), 2)}; cut below a quarter {100*(r < .25).mean():.0f}%; "
              f"pools ${sum(m0[c]['rate'] for c in cs):.0f} -> ${sum(s[c]['rate'] for c in cs):.0f}")
    if came:
        q = np.array([s[c]["comp"] * s[c]["rate"] for c in came]); rate = np.array([s[c]["rate"] for c in came])
        rew = rate * ours / (q + ours)
        print(f"   where somebody came: total score median {np.median(q):.0f} points (ours would be {ours:.0f}); a min-size quote would now earn median ${np.median(rew):.2f}/day, "
              f"all of them ${rew.sum():.0f}/day (the pools were ${sum(m0[c]['rate'] for c in came):.0f}/day while empty)")
