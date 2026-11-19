"""Replay of a "fair value" taker on 5m windows: at the first public trade of a window (t) where the side bought at px was
>= TH below the Binance fair value seen L s earlier (Binance strike = mean of [T-62, T-3], sigma 1 h of 10 s returns), we buy
`sh` shares at px (the price someone else got there; we would need to be first). Window zone: x in [X_LO, X_HI] s before the end.
One entry per window. Prints entries a day, win %, $ per day at 10 shares, by day / asset / price."""
import json, collections, math, time, sys, numpy as np
from zmod import PX, T0, strike
W = {w["slug"]: w for w in json.load(open("windows.json"))}
MT = json.load(open("mtrades.json"))
def fee(p): return 0.07 * p * (1 - p)
LP, SIG = {}, {}
for a, p in PX.items():
    lp = np.log(p); LP[a] = lp
    r10 = np.zeros_like(lp); r10[10:] = (lp[10:] - lp[:-10])
    c = np.concatenate([[0.0], np.cumsum(r10 * r10)])
    n = np.arange(len(lp)); lo = np.maximum(n - 3600, 0)
    SIG[a] = np.sqrt((c[n] - c[lo]) / np.maximum(n - lo, 1) / 10.0)
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
def run(kind="5m", TH=0.10, L=1, X_LO=62, X_HI=240, PMIN=0.05, PMAX=0.95, assets=None, max_per_window=1, buys_only=True):
    ent = []
    for slug, trs in MT.items():
        w = W.get(slug)
        if not w or not trs or w["kind"] != kind or (assets and w["asset"] not in assets): continue
        a, end = w["asset"], w["end"]
        K = strike(w); lk = math.log(K); lp, sg = LP[a], SIG[a]
        got = 0
        for t, sd, o, sz, px, wal in sorted(trs, key=lambda r: r[0]):
            x = end - t
            if not X_LO <= x <= X_HI or not PMIN <= px <= PMAX: continue
            if buys_only and sd != "B": continue
            i = int(t) - T0; j = i - L
            if j < 3610 or i >= len(lp): continue
            s = sg[j]
            if s <= 0: continue
            z = (lp[j] - lk) / (s * math.sqrt((x + L) - 62 + 59 / 3))
            pu = Phi(z); po = pu if o == "Up" else 1 - pu
            bo = o if sd == "B" else ("Down" if o == "Up" else "Up")        # a taker SELL of o = we buy the other side at 1 - px
            bp = px if sd == "B" else 1 - px
            pb = po if sd == "B" else 1 - po
            if pb - bp < TH: continue
            won = bo == w["win"]
            ent.append(dict(t=t, a=a, px=bp, won=won, e=(1.0 if won else 0.0) - bp - fee(bp), x=x, me=pb - bp))
            got += 1
            if got >= max_per_window: break
    return ent
def report(ent, label, sh=10):
    if not ent: print(label, "no entries"); return
    n = len(ent); win = sum(e["won"] for e in ent); usd = sum(e["px"] for e in ent) * sh; pnl = sum(e["e"] for e in ent) * sh
    days = collections.defaultdict(float)
    for e in ent: days[time.strftime("%m-%d", time.gmtime(e["t"]))] += e["e"] * sh
    print(f"{label}: {n} entries ({n/8:.0f}/day), won {100*win/n:.1f}%, avg px {sum(e['px'] for e in ent)/n:.3f}, "
          f"ROI {100*pnl/usd:+.1f}%, ${pnl/8:+.1f}/day at {sh} sh; days + {sum(1 for v in days.values() if v>0)}/{len(days)}: "
          + " ".join(f"{k[3:]}:{v:+.0f}" for k, v in sorted(days.items())))
if __name__ == "__main__":
    for TH in (0.08, 0.10, 0.15, 0.20):
        for L in (1, 2, 3):
            report(run(TH=TH, L=L), f"5m TH {TH:.2f} lag {L}s")
