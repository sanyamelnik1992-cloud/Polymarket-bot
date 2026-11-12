"""Min-size two-sided reward quote with an instant hedge, replayed on 7 days of minute prices and taker trades.

Per market and distance d (cents from the mid): the YES bid is floor(mid - d), the YES ask (a NO bid) ceil(mid + d), mid = the
price LAG seconds before the trade (a bot does not re-centre instantly). A taker print strictly THROUGH our price fills us
(variant: at-or-through). A fill is hedged at once at one tick worse than the print (the level the sweep reached) plus the taker
fee; the market then rests COOL seconds. Reward per quoted minute = rate / 1440 * Q / (competitiveness * rate + Q),
Q = size * ((v - d_eff) / v)^2 (the official competitiveness = total score per $ of daily pool, see comp2.py).
usage: sim_mk.py MK7.jsonl CANDS.pkl OUT.pkl"""
import bisect, json, math, sys
import numpy as np, pandas as pd
LAG, COOL, TICK = 30, 120, 0.01
FEE = 0.05                                    # taker fee rate of most categories (0.04 finance/politics/tech, 0 geopolitics)
DISTS = (1.0, 2.0, 3.0, 4.0)
k = pd.read_pickle(sys.argv[2]).set_index("cid")
rows = []
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r["cid"] not in k.index: continue
    m = k.loc[r["cid"]]
    mids = r["mid"]; tr = r["trades"]
    if len(mids) < 200: continue
    mt = [x[0] for x in mids]; mp = [x[1] for x in mids]
    t0, t1 = mt[0], mt[-1]
    span_d = (t1 - t0) / 86400
    if span_d < 2: continue
    half = t0 + (t1 - t0) / 2
    def mid_at(t):
        i = bisect.bisect_right(mt, t) - 1
        return mp[i] if i >= 0 else None
    S = float(m.ms); v = float(m.v); rate = float(m["rate"]); qtot = float(m.comp) * rate
    for d in DISTS:
        if d >= v: continue
        for touch in (0, 1):
            st = {h: dict(fills=0, shares=0.0, usd=0.0, loss=0.0, loss1m=0.0, loss10m=0.0, cool=0.0) for h in (0, 1)}
            until = 0
            for (ts, py, size, buys_yes) in tr:
                if ts < t0 + LAG or ts < until: continue
                ref = mid_at(ts - LAG)
                if ref is None or not (0.10 <= ref <= 0.90): continue
                b = math.floor((ref - d / 100) / TICK + 1e-9) * TICK
                a = math.ceil((ref + d / 100) / TICK - 1e-9) * TICK
                eps = 1e-9 if touch else -1e-9
                h = 0 if ts < half else 1
                if not buys_yes and py < b + eps:           # a taker sold YES through our bid
                    f = min(S, size); hp = max(py - TICK, 0.001)
                    loss = (b - hp) + FEE * hp * (1 - hp)
                    m1, m10 = mid_at(ts + 60), mid_at(ts + 600)
                    l1 = (b - m1) if m1 is not None else 0.0; l10 = (b - m10) if m10 is not None else 0.0
                    px = b
                elif buys_yes and py > a - eps:             # a taker bought YES through our ask
                    f = min(S, size); hp = min(py + TICK, 0.999)
                    loss = (hp - a) + FEE * hp * (1 - hp)
                    m1, m10 = mid_at(ts + 60), mid_at(ts + 600)
                    l1 = (m1 - a) if m1 is not None else 0.0; l10 = (m10 - a) if m10 is not None else 0.0
                    px = 1 - a
                else:
                    continue
                s = st[h]; s["fills"] += 1; s["shares"] += f; s["usd"] += f * px; s["loss"] += f * loss; s["loss1m"] += f * l1; s["loss10m"] += f * l10
                s["cool"] += min(COOL, t1 - ts); until = ts + COOL
            # quoted minutes: mid inside [0.10, 0.90]
            for h, (ha, hb) in enumerate(((t0, half), (half, t1))):
                idx = [i for i, t in enumerate(mt) if ha <= t < hb]
                inr = sum(1 for i in idx if 0.10 <= mp[i] <= 0.90)
                days = (hb - ha) / 86400
                up = max(0.0, inr / max(len(idx), 1) - st[h]["cool"] / (hb - ha))
                # effective distance after rounding to the tick (mid mostly on a half tick or a tick): use d
                Q = S * ((v - d) / v) ** 2
                rew = rate * Q / (qtot + Q) * up * days
                s = st[h]
                # realised volatility of the mid (per day) and trend, for selection features
                rows.append(dict(cid=r["cid"], d=d, touch=touch, half=h, days=days, fills=s["fills"], shares=s["shares"], usd=s["usd"], loss=s["loss"],
                                 loss1m=s["loss1m"], loss10m=s["loss10m"], rew=rew, up=up, S=S, v=v, rate=rate, comp=float(m.comp), p=float(m.p),
                                 spread=float(m.spread), vol24=float(m.vol), ntr=sum(1 for x in tr if ha <= x[0] < hb),
                                 rng=(max(mp[i] for i in idx) - min(mp[i] for i in idx)) if idx else 0.0, q=m.q))
o = pd.DataFrame(rows); o.to_pickle(sys.argv[3])
print("markets simulated:", o.cid.nunique(), "rows", len(o))
