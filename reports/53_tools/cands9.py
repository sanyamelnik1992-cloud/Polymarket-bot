"""Lock-entry candidates on the closing-minute tapes (dl_lastmin.py) with the production model on Binance 1 s closes as the
Chainlink proxy (the method of reports/47_tools/lockcands.py), plus extra features and fill prices for several sizes.
A candidate = second s in [240, 296] where the book-independent state filters of decide_state() pass with p >= 0.80, and
at least 5 shares of the model's side were bought by takers in [s+1, s+2] (the asks our FAK would have hit).
usage: cands9.py ASSET TAPE.jsonl BINANCE.json OUT.json"""
import os
import json, math, sys
import numpy as np
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.lockmodel import Tables, ClSigma, lock_state, prob_up, expected_dist_bp  # noqa: E402

asset, tape, bnf, out = sys.argv[1:5]
tables = Tables(os.path.join(ROOT, "config", "lock_tables.npz"))
bn = {int(k): v for k, v in json.load(open(bnf)).items()}
t0, t1 = min(bn), max(bn)
arr = np.full(t1 - t0 + 1, np.nan)
for k, v in bn.items():
    arr[k - t0] = v
for i in range(1, len(arr)):
    if np.isnan(arr[i]):
        arr[i] = arr[i - 1]
cs = ClSigma()
sig = np.full(len(arr), np.nan)
lr = np.zeros(len(arr))
lr[1:] = np.diff(np.log(arr))
for i in range(len(arr)):
    cs.on_print(t0 + i, arr[i])
    sig[i] = cs.sigma() or np.nan
# short-horizon realized vol (rms of the last 30 / 60 one-second returns)
c2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
def rms(i, n):
    return math.sqrt(max(c2[i + 1] - c2[i + 1 - n], 0.0) / n)
P = lambda t: float(arr[t - t0])
SIZES = (5, 10, 20, 50, 100, 200)
res, nwin = [], 0
for line in open(tape):
    w = json.loads(line)
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down") or ws - 1000 < t0 or ws + 300 > t1:
        continue
    nwin += 1
    K = float(np.mean([P(s) for s in range(ws - 62, ws - 2)]))
    # taker BUYs of each outcome per second: (price, size). A taker SELL of X is a taker BUY of the other side at 1-p
    ex = {"Up": {}, "Down": {}}
    for (t, wal, side, o, size, p, tk, tx) in w["trades"]:
        if not tk:
            continue
        s = t - ws
        if side == "BUY":
            ex[o].setdefault(s, []).append((p, size))
        else:
            ex["Down" if o == "Up" else "Up"].setdefault(s, []).append((1 - p, size))
    state = {}
    for s in range(236, 297):
        now = ws + s
        i = now - 1 - t0
        sg = sig[i]
        if not np.isfinite(sg) or sg <= 0:
            continue
        cl = {u: P(u) for u in range(ws + 238, now)}
        st = lock_state(cl, now - 1, P(now - 1), ws, K, float(now), P(now), P(now - 1))
        p_nc = prob_up(tables, asset, st["A_nc"], st["m_nc"], st["S_nc"], K, sg)
        state[s] = (p_nc, st, sg)
    for s in range(240, 297):
        if s not in state:
            continue
        now = ws + s
        p_nc, st, sg = state[s]
        up = p_nc >= 0.5
        sgn = 1.0 if up else -1.0
        p = p_nc if up else 1 - p_nc
        if p < 0.80:
            continue
        dist = sgn * expected_dist_bp(st["A_nc"], st["m_nc"], st["S_nc"], K)
        spot = sgn * (st["S_nc"] / K - 1) * 1e4
        mv1 = math.log(P(now) / P(now - 1)) * 1e4
        mv3 = math.log(P(now) / P(now - 3)) * 1e4
        if dist < 0.5 or spot < 0 or -sgn * mv1 > 1.0 or -sgn * mv3 > 2.0 or abs(mv1) > 5.0:
            continue
        tok = "Up" if up else "Down"
        fills = [f for u in (s + 1, s + 2) for f in ex[tok].get(u, [])]      # time order, as they printed
        if sum(q for _, q in fills) < 5 - 1e-9:
            continue
        px = {}
        for n in SIZES:
            got = cost = 0.0
            for (pp, q) in fills:
                x = min(q, n - got)
                got += x
                cost += x * pp
                if got >= n - 1e-9:
                    break
            px[n] = round(cost / got, 4) if got >= n - 1e-9 else None
        # the same side's model probability 1, 2, 3 s earlier (how long the signal has been there)
        prev = [round((state[s - k][0] if up else 1 - state[s - k][0]), 4) if s - k in state else None for k in (1, 2, 3)]
        # last taker price of our side in the 2 s before the signal (what the market paid before us)
        before = [pp for u in (s - 1, s) for (pp, q) in ex[tok].get(u, [])]
        ii = now - t0
        res.append(dict(a=asset, ws=ws, s=s, p=round(p, 5), dist=round(dist, 3), spot=round(spot, 3), won=tok == win,
                        sig=round(sg * 1e4, 4), v30=round(rms(ii, 30) * 1e4, 4), v60=round(rms(ii, 60) * 1e4, 4),
                        mv1=round(sgn * mv1, 2), mv3=round(sgn * mv3, 2), mv10=round(sgn * math.log(P(now) / P(now - 10)) * 1e4, 2),
                        mv30=round(sgn * math.log(P(now) / P(now - 30)) * 1e4, 2), m=st["m_nc"], prev=prev,
                        pxb=round(before[-1], 3) if before else None,
                        px=px, vol=round(sum(q for _, q in fills), 1)))
json.dump({"asset": asset, "windows": nwin, "cands": res}, open(out, "w"))
print(asset, "windows", nwin, "candidates", len(res), flush=True)
