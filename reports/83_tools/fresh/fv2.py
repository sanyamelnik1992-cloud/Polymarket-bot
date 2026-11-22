"""Replay of the Перехватчик rule on all public taker trades (18 Nov 01:00 - 22 Nov): a public trade of a window is "ours"
when the side it bought sat >= TH below the Binance fair value seen L s earlier (strike = mean Binance of [T-62, T-3], sigma =
RMS of 10 s returns over 1 h per 1 s, tau = s to the closing TWAP + 19.5). Upper bound: we would have to be first.
Run from the data folder (bn1s.npz, windows.json, trades.json)."""
import json, collections, math, time, sys
import numpy as np
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
from zmod import PX, T0
W = {w["slug"]: w for w in json.load(open("windows.json"))}
TR = json.load(open("trades.json"))
def fee(p): return 0.07 * p * (1 - p)
LP, SIG = {}, {}
for a, p in PX.items():
    lp = np.log(p); LP[a] = lp
    r10 = np.zeros_like(lp); r10[10:] = (lp[10:] - lp[:-10])
    c = np.concatenate([[0.0], np.cumsum(r10 * r10)])
    n = np.arange(len(lp)); lo = np.maximum(n - 3600, 0)
    SIG[a] = np.sqrt((c[n] - c[lo]) / np.maximum(n - lo, 1) / 10.0)
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
def strike(w):                     # mean Binance of [start-62, start-3]: the window's Chainlink strike seconds (5m and 15m)
    return PX[w["asset"]][w["start"] - 62 - T0: w["start"] - 2 - T0].mean()
def events(kind, L):
    out = []
    for slug, trs in TR.items():
        w = W.get(slug)
        if not w or not trs or w["kind"] != kind: continue
        a, end = w["asset"], w["end"]
        if w["start"] - 70 - T0 < 3600: continue
        K = strike(w); lk = math.log(K); lp, sg = LP[a], SIG[a]
        for t, sd, o, sz, px, wal in trs:
            x = end - t
            if not 62 <= x <= (290 if kind == "5m" else 840): continue
            i = int(t) - T0; j = i - L
            if j < 3610 or i >= len(lp): continue
            s = sg[j]
            if s <= 0: continue
            z = (lp[j] - lk) / (s * math.sqrt(x + L - 62 + 19.5))
            pu = Phi(z); po = pu if o == "Up" else 1 - pu
            if sd == "B": bo, bp, pb = o, px, po
            else: bo, bp, pb = ("Down" if o == "Up" else "Up"), 1 - px, 1 - po
            out.append(dict(slug=slug, t=t, a=a, x=x, side=bo, px=bp, p=pb, edge=pb - bp, won=bo == w["win"], sz=sz, wal=wal, sd=sd))
    out.sort(key=lambda e: (e["slug"], e["t"]))
    return out
def pick(evs, TH, PMIN=0.05, PMAX=0.95, max_per_window=1, xlo=62, xhi=290, assets=None, buys_only=True):
    ent, cnt = [], collections.Counter()
    for e in evs:
        if e["edge"] < TH or not PMIN <= e["px"] <= PMAX or not xlo <= e["x"] <= xhi: continue
        if assets and e["a"] not in assets: continue
        if buys_only and e["sd"] != "B": continue
        if cnt[e["slug"]] >= max_per_window: continue
        cnt[e["slug"]] += 1
        ent.append(e)
    return ent
def rep(ent, label, sh=10):
    if not ent: print(f"{label}: none"); return
    n = len(ent); win = sum(e["won"] for e in ent); cost = sum(e["px"] + fee(e["px"]) for e in ent)
    pnl = sum((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"]) for e in ent)
    days = collections.defaultdict(float)
    for e in ent: days[time.strftime("%d", time.gmtime(e["t"]))] += ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) * sh
    nd = len(days)
    print(f"{label:34s} {n:5d} ({n/nd:4.0f}/d) won {100*win/n:5.1f}% exp {100*sum(e['px'] for e in ent)/n:5.1f}% px {sum(e['px'] for e in ent)/n:.3f} "
          f"ROI {100*pnl/cost:+6.1f}% ${pnl*sh/nd:+6.1f}/d | " + " ".join(f"{k}:{v:+.0f}" for k, v in sorted(days.items())))
if __name__ == "__main__":
    for kind in ("5m", "15m"):
        for L in (1, 2):
            ev = events(kind, L)
            xhi = 290 if kind == "5m" else 840
            print(f"--- {kind} lag {L}s: {len(ev)} trades in the zone")
            for TH in (0.10, 0.12, 0.15, 0.20, 0.25):
                rep(pick(ev, TH, xhi=xhi), f"{kind} L{L} TH {TH:.2f} first/window")
            rep(pick(ev, 0.15, max_per_window=3, xhi=xhi), f"{kind} L{L} TH 0.15 up to 3/window")
