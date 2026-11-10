"""Per hour of 10 Nov: closing-minute state seconds by rule variant (Binance proxy), and the volatility level.
A 'state second' = second 240-296 of a window where the production state filters pass for a coin; with fills = takers bought
our side within the tier limit in the next 2 s (an L3 tape entry). Variants: plain L3; sigma floor 0.5 bp + floor_free_p 0.99;
+ shock pause (3 s move > 2.5 bp ended in the last 10 s).
usage: today.py DIR   (DIR holds lm_<coin>.jsonl from 53_tools/dl_lastmin.py and bn_<SYMBOL>.json from 46_tools/dl_binance.py)"""
import json, math, sys, collections
import numpy as np
import os
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.lockmodel import Tables, ClSigma, lock_state, prob_up, expected_dist_bp
tables = Tables(os.path.join(ROOT, "config", "lock_tables.npz"))
D = sys.argv[1]
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def lim_for(p):
    best = None
    for pm, am in TIERS:
        if p >= pm and (best is None or am > best): best = am
    return best
hours = collections.defaultdict(lambda: collections.Counter())
sigh = collections.defaultdict(list)
for a, s in SYM.items():
    bn = {int(k): v for k, v in json.load(open(f"{D}/bn_{s}.json")).items()}
    t0, t1 = min(bn), max(bn)
    arr = np.full(t1 - t0 + 1, np.nan)
    for k, v in bn.items(): arr[k - t0] = v
    for i in range(1, len(arr)):
        if np.isnan(arr[i]): arr[i] = arr[i - 1]
    cs = ClSigma(); sig = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        cs.on_print(t0 + i, arr[i]); sig[i] = cs.sigma() or np.nan
    P = lambda t: float(arr[t - t0])
    for line in open(f"{D}/lm_{a}.jsonl"):
        w = json.loads(line); ws, win = w["ws"], w["winner"]
        if win not in ("Up", "Down") or ws - 1000 < t0 or ws + 300 > t1: continue
        h = (ws % 86400) // 3600
        K = float(np.mean([P(u) for u in range(ws - 62, ws - 2)]))
        ex = {"Up": {}, "Down": {}}
        for (t, wal, side, o, size, p, tk, tx) in w["trades"]:
            if not tk: continue
            s_ = t - ws
            if side == "BUY": ex[o].setdefault(s_, []).append((p, size))
            else: ex["Down" if o == "Up" else "Up"].setdefault(s_, []).append((1 - p, size))
        sigh[h].append(sig[ws + 270 - t0] * 1e4)
        done = {"plain": False, "floor": False, "shock": False}
        seen = {"plain": False, "floor": False, "shock": False}
        for s_ in range(240, 297):
            now = ws + s_
            sg_raw = sig[now - 1 - t0]
            if not np.isfinite(sg_raw): continue
            cl = {u: P(u) for u in range(ws + 238, now)}
            st = lock_state(cl, now - 1, P(now - 1), ws, K, float(now), P(now), P(now - 1))
            mv1 = math.log(P(now) / P(now - 1)) * 1e4; mv3 = math.log(P(now) / P(now - 3)) * 1e4
            shock = max(abs(math.log(P(now - k) / P(now - k - 3))) * 1e4 for k in range(0, 11))
            for var in ("plain", "floor", "shock"):
                sg = sg_raw if var == "plain" else max(sg_raw, 0.5e-4)
                p_nc = prob_up(tables, a, st["A_nc"], st["m_nc"], st["S_nc"], K, sg)
                up = p_nc >= 0.5; sgn = 1.0 if up else -1.0; p = p_nc if up else 1 - p_nc
                if var != "plain" and sg > sg_raw:
                    q = prob_up(tables, a, st["A_nc"], st["m_nc"], st["S_nc"], K, sg_raw); q = q if up else 1 - q
                    if q >= 0.99: p = q
                if p < 0.90: continue
                dist = sgn * expected_dist_bp(st["A_nc"], st["m_nc"], st["S_nc"], K); spot = sgn * (st["S_nc"] / K - 1) * 1e4
                if dist < 0.5 or spot < 0 or -sgn * mv1 > 1.0 or -sgn * mv3 > 2.0 or abs(mv1) > 5.0: continue
                if var == "shock" and shock > 2.5: continue
                if not seen[var]:
                    seen[var] = True; hours[h][var + "_state"] += 1          # windows where the model state ever qualifies
                if done[var]: continue
                tok = "Up" if up else "Down"; lim = lim_for(p)
                fills = [f for u in (s_ + 1, s_ + 2) for f in ex[tok].get(u, [])]
                got = cost = 0.0
                for (pp, q_) in fills:
                    x = min(q_, 5 - got); got += x; cost += x * pp
                    if got >= 5 - 1e-9: break
                if got >= 5 - 1e-9 and 0.90 <= cost / got <= lim:
                    done[var] = True; hours[h][var] += 1; hours[h][var + "_lost"] += (tok != win)
        hours[h]["windows"] += 1
print("hour UTC | sigma bp (median of the 4 coins) | windows where the model qualifies: plain / floor / floor+shock | tape entries: plain / floor / floor+shock (lost)")
for h in sorted(hours):
    c = hours[h]; sg = sorted(x for x in sigh[h] if np.isfinite(x))
    print(f"   {h:02d}    |  {sg[len(sg)//2]:.2f}  |  {c['plain_state']:3d} / {c['floor_state']:3d} / {c['shock_state']:3d}  |  "
          f"{c['plain']:2d} ({c['plain_lost']}) / {c['floor']:2d} ({c['floor_lost']}) / {c['shock']:2d} ({c['shock_lost']})")
