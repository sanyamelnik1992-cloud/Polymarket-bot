"""Collect lock-entry CANDIDATES per window on public tapes (Binance 1 s as the Chainlink proxy, taker executions of the decided
token as the fillable asks). A candidate = (second s in [240,295] where the book-independent L state filters pass, p >= 0.80)
x (5 shares fillable at VWAP price in [s+0.3, s+4] s). Rules are applied later (tiers.py) on this list.
usage: lockcands.py ASSET OUT.json WINDOWS[,WINDOWS..] BINANCE[,BINANCE..]"""
import json, math, os, sys
import numpy as np
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.lockmodel import Tables, ClSigma, lock_state, prob_up, expected_dist_bp
asset, out = sys.argv[1], sys.argv[2]
wfiles, bfiles = sys.argv[3].split(","), sys.argv[4].split(",")
tables = Tables(os.path.join(ROOT, "config", "lock_tables.npz"))
bn = {}
for f in bfiles: bn.update({int(k): v for k, v in json.load(open(f)).items()})
t0, t1 = min(bn), max(bn)
arr = np.full(t1 - t0 + 1, np.nan)
for k, v in bn.items(): arr[k - t0] = v
for i in range(1, len(arr)):
    if np.isnan(arr[i]): arr[i] = arr[i - 1]
cs = ClSigma(); sigarr = np.full(len(arr), np.nan)
for i in range(len(arr)): cs.on_print(t0 + i, arr[i]); sigarr[i] = cs.sigma() or np.nan
P = lambda t: float(arr[t - t0])
def other(o): return "Down" if o == "Up" else "Up"
SH = 5.0
import os
WIN = float(os.environ.get('LOCK_WIN', '2.0'))
res, nwin = [], 0
for wf in wfiles:
    for line in open(wf):
        w = json.loads(line); ws, win = w["ws"], w["winner"]
        if win not in ("Up", "Down") or ws - 70 < t0 or ws + 300 > t1: continue
        nwin += 1
        K = float(np.mean([P(s) for s in range(ws - 62, ws - 2)]))
        ex = []
        for (t, wal, side, o, size, p, tk) in w["trades"]:
            if not tk or t - ws < 240: continue
            ex.append((t - ws, o, p, size) if side == "BUY" else (t - ws, other(o), 1 - p, size))
        if not ex: continue
        for s in range(240, 296):
            now = ws + s; sg = sigarr[now - 1 - t0]
            if not np.isfinite(sg): continue
            cl = {u: P(u) for u in range(ws + 238, now)}
            st = lock_state(cl, now - 1, P(now - 1), ws, K, float(now), P(now), P(now - 1))
            p_nc = prob_up(tables, asset, st["A_nc"], st["m_nc"], st["S_nc"], K, sg)
            up = p_nc >= 0.5; sgn = 1.0 if up else -1.0; p = p_nc if up else 1 - p_nc
            if p < 0.80: continue
            dist = sgn * expected_dist_bp(st["A_nc"], st["m_nc"], st["S_nc"], K); spot = sgn * (st["S_nc"] / K - 1) * 1e4
            mv1 = math.log(P(now) / P(now - 1)) * 1e4; mv3 = math.log(P(now) / P(now - 3)) * 1e4
            if dist < 0.5 or spot < 0 or -sgn * mv1 > 1.0 or -sgn * mv3 > 2.0 or abs(mv1) > 5.0: continue
            tok = "Up" if up else "Down"
            # fills in TIME order inside [s+0.3, s+WIN]: the first executions after our order arrives are the asks it would hit
            # (the earlier version took the cheapest execution of 4 s, which flatters rules that need cheap asks)
            fills = sorted((e for e in ex if e[1] == tok and s + 0.3 <= e[0] <= s + WIN), key=lambda e: e[0])
            got = cost = 0.0
            for (_, _, px, size) in fills:
                q = min(size, SH - got); got += q; cost += q * px
                if got >= SH - 1e-9: break
            if got < SH - 1e-9: continue
            res.append(dict(asset=asset, ws=ws, s=s, p=round(p, 5), dist=round(dist, 3), px=round(cost / got, 4), won=tok == win,
                            sig=round(sg * 1e4, 3), mv1=round(sgn * mv1, 2), mv3=round(sgn * mv3, 2)))
json.dump({"asset": asset, "windows": nwin, "cands": res}, open(out, "w"))
print(asset, "windows", nwin, "candidates", len(res))
