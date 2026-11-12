import json, sys, time, collections
import numpy as np
d = json.load(open(sys.argv[1])); rows = d["rows"]
today = time.strftime("%Y-%m-%d", time.gmtime(d["ts"]))
seen = {}; dup = 0
for m in rows:
    c = m.get("condition_id")
    if c in seen: dup += 1; continue
    seen[c] = m
ms = list(seen.values())
print("rows", len(rows), "unique", len(ms), "dups", dup, "today", today)
print("keys:", sorted(ms[0].keys()))
print("sample:", json.dumps({k: v for k, v in ms[0].items() if k not in ("image",)}, ensure_ascii=False)[:1500])
def active(m):
    out = []
    for c in m.get("rewards_config") or []:
        if (c.get("start_date") or "0000") <= today <= (c.get("end_date") or "9999"):
            out.append(c)
    return out
ncfg = collections.Counter(len(m.get("rewards_config") or []) for m in ms)
print("configs per market:", dict(sorted(ncfg.items())[:8]))
act = [(m, active(m)) for m in ms]
act = [(m, a) for m, a in act if a and sum(float(c.get("rate_per_day") or 0) for c in a) > 0]
print("markets with an active config and rate > 0:", len(act))
by_asset = collections.defaultdict(lambda: [0, 0.0])
for m, a in act:
    for c in a:
        by_asset[c.get("asset_address")][0] += 1; by_asset[c.get("asset_address")][1] += float(c.get("rate_per_day") or 0)
for k, v in sorted(by_asset.items(), key=lambda x: -x[1][1]): print("  asset", k, "configs", v[0], "rate/day", round(v[1], 1))
rate = np.array([sum(float(c.get("rate_per_day") or 0) for c in a) for m, a in act])
print("total rate/day:", round(rate.sum(), 0), " quantiles 10/25/50/75/90/99:", np.round(np.quantile(rate, [.1, .25, .5, .75, .9, .99]), 2))
for lo, hi in ((0, 1), (1, 5), (5, 20), (20, 50), (50, 100), (100, 300), (300, 1000), (1000, 1e9)):
    k = (rate >= lo) & (rate < hi); print(f"  rate [{lo},{hi}): markets {k.sum():6d}  pool {rate[k].sum():10.0f}")
for f in ("rewards_min_size", "rewards_max_spread"):
    cnt = collections.Counter(float(m.get(f) or 0) for m, a in act)
    print(f, dict(cnt.most_common(12)))
comp = np.array([float(m.get("market_competitiveness") or 0) for m, a in act])
print("competitiveness quantiles 0/10/25/50/75/90/99/100:", np.round(np.quantile(comp, [0, .1, .25, .5, .75, .9, .99, 1]), 2), " zero:", int((comp == 0).sum()))
vol = np.array([float(m.get("volume_24hr") or 0) for m, a in act])
print("volume_24hr quantiles 10/25/50/75/90/99:", np.round(np.quantile(vol, [.1, .25, .5, .75, .9, .99]), 0), " zero:", int((vol == 0).sum()))
spr = np.array([float(m.get("spread") or 0) for m, a in act])
print("spread quantiles 10/25/50/75/90:", np.round(np.quantile(spr, [.1, .25, .5, .75, .9]), 3))
json.dump([dict(m, _rate=float(r)) for (m, a), r in zip(act, rate)], open("active.json", "w"))
