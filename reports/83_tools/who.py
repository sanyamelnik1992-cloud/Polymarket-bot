"""Who takes the stale asks (5m, taker BUY, model edge >= 0.15 with Binance 2 s earlier): wallet concentration, trade size,
how long the mispricing lasts inside a window (trades per window, seconds from the first to the last such trade)."""
import json, collections, math, numpy as np
from zmod import PX, T0, strike
W = {w["slug"]: w for w in json.load(open("windows.json"))}
MT = json.load(open("mtrades.json"))
LP, SIG = {}, {}
for a, p in PX.items():
    lp = np.log(p); LP[a] = lp
    r10 = np.zeros_like(lp); r10[10:] = (lp[10:] - lp[:-10])
    c = np.concatenate([[0.0], np.cumsum(r10 * r10)])
    n = np.arange(len(lp)); lo = np.maximum(n - 3600, 0)
    SIG[a] = np.sqrt((c[n] - c[lo]) / np.maximum(n - lo, 1) / 10.0)
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
byw = collections.Counter(); usd_w = collections.Counter(); sizes = []; per_window = []; spans = []
for slug, trs in MT.items():
    w = W.get(slug)
    if not w or w["kind"] != "5m" or not trs: continue
    a, end = w["asset"], w["end"]; K = strike(w); lk = math.log(K); lp, sg = LP[a], SIG[a]
    hits = []
    for t, sd, o, sz, px, wal in trs:
        x = end - t
        if sd != "B" or not 62 <= x <= 240 or not 0.05 <= px <= 0.95: continue
        j = int(t) - T0 - 2
        if j < 3610: continue
        z = (lp[j] - lk) / (sg[j] * math.sqrt((x + 2) - 62 + 59 / 3))
        pu = Phi(z); po = pu if o == "Up" else 1 - pu
        if po - px >= 0.15:
            hits.append(t); byw[wal] += 1; usd_w[wal] += sz * px; sizes.append(sz)
    if hits:
        per_window.append(len(hits)); spans.append(max(hits) - min(hits))
tot = sum(byw.values()); tot_usd = sum(usd_w.values())
print("stale-ask buys:", tot, "$%.0f" % tot_usd, "wallets:", len(byw), "windows with any:", len(per_window), "of 9216")
top = usd_w.most_common(15)
print("top 15 wallets by $:", [(w, round(v), byw[w]) for w, v in top])
print("share of $ by top 5 / 20 wallets: %.1f%% / %.1f%%" % (100 * sum(v for _, v in usd_w.most_common(5)) / tot_usd, 100 * sum(v for _, v in usd_w.most_common(20)) / tot_usd))
s = sorted(sizes); print("trade size shares p25/p50/p75/p90:", s[len(s)//4], s[len(s)//2], s[3*len(s)//4], s[9*len(s)//10])
pw = sorted(per_window); sp = sorted(spans)
print("such buys per window p50/p75/p90:", pw[len(pw)//2], pw[3*len(pw)//4], pw[9*len(pw)//10], " seconds first->last p50/p75/p90:", sp[len(sp)//2], sp[3*len(sp)//4], sp[9*len(sp)//10])
