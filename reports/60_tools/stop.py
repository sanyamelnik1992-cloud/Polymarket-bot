"""Does cutting a lock entry help? After an entry of the reports/59 rule, sell (or buy the other side, the same thing) when the
model's probability of our side falls to X or lower. Exit price = what takers who sold our side got in the next 2 s on the tape
(time order, N shares); no such prints in 6 s = no exit, the entry is held. Model on Binance 1 s closes, as in cands9.py.
usage: stop.py SHARES TAPE_DIR(lm_<asset>.jsonl, bn_<SYMBOL>.json) S9_btc.json S9_eth.json S9_sol.json S9_xrp.json"""
import collections, json, math, os, sys
import numpy as np
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.lockmodel import Tables, ClSigma, lock_state, prob_up  # noqa: E402
N = float(sys.argv[1]); D = sys.argv[2]; KEY = str(int(N))
fee = lambda p: 0.07 * p * (1 - p)
tables = Tables(os.path.join(ROOT, "config", "lock_tables.npz"))
C = []
for f in sys.argv[3:]:
    C += json.load(open(f))["cands"]
TOP = ((.99, .98), (.97, .95), (.95, .93)); LOW = (.90, .91)
def lim_for(p, low_ok):
    best = None
    for pm, am in TOP + ((LOW,) if low_ok else ()):
        if p >= pm and (best is None or am > best): best = am
    return best
def ok(c):
    low_ok = c["s"] >= 260
    px = c["px"].get(KEY); lim = lim_for(c["p"], low_ok)
    if px is None or lim is None or not (0.90 <= px <= lim): return False
    pp = c["prev"][0]
    return pp is not None and lim_for(pp, low_ok) is not None and px <= lim_for(pp, low_ok)
by = collections.defaultdict(list)
for c in C: by[(c["a"], c["ws"])].append(c)
entries = {}
for k, v in by.items():
    v.sort(key=lambda c: c["s"])
    for c in v:
        if c["sh"] <= max(2.5, 3.0 * c["sig"] * math.sqrt(3)) and ok(c):
            entries[k] = c; break
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
paths = []                                                   # per entry: (cand, {second: p of our side}, {second: [(price, size)] of bids hit})
for a in SYM:
    want = {ws for (aa, ws) in entries if aa == a}
    if not want: continue
    bn = {int(k): v for k, v in json.load(open(os.path.join(D, f"bn_{SYM[a]}.json"))).items()}
    t0, t1 = min(bn), max(bn)
    arr = np.full(t1 - t0 + 1, np.nan)
    for k, v in bn.items(): arr[k - t0] = v
    for i in range(1, len(arr)):
        if np.isnan(arr[i]): arr[i] = arr[i - 1]
    cs = ClSigma(); sig = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        cs.on_print(t0 + i, arr[i]); sig[i] = cs.sigma() or np.nan
    P = lambda t: float(arr[t - t0])
    for line in open(os.path.join(D, f"lm_{a}.jsonl")):
        w = json.loads(line)
        if w["ws"] not in want: continue
        ws = w["ws"]; c = entries[(a, ws)]
        up = None
        K = float(np.mean([P(s) for s in range(ws - 62, ws - 2)]))
        pp = {}
        for s in range(c["s"], 297):
            now = ws + s
            cl = {u: P(u) for u in range(ws + 238, now)}
            st = lock_state(cl, now - 1, P(now - 1), ws, K, float(now), P(now), P(now - 1))
            p_nc = prob_up(tables, a, st["A_nc"], st["m_nc"], st["S_nc"], K, sig[now - 1 - t0])
            if up is None: up = p_nc >= 0.5
            pp[s] = p_nc if up else 1 - p_nc
        tok = "Up" if up else "Down"
        hit = collections.defaultdict(list)                  # bids for our side that takers hit
        for (t, wal, side, o, size, p, tk, tx) in w["trades"]:
            if not tk: continue
            if side == "SELL" and o == tok: hit[t - ws].append((p, size))
            elif side == "BUY" and o != tok: hit[t - ws].append((1 - p, size))
        paths.append((c, pp, hit))
def exit_px(hit, s):
    got = cost = 0.0
    for u in range(s + 1, s + 7):
        for (p, q) in hit.get(u, []):
            x = min(q, N - got); got += x; cost += x * p
            if got >= N - 1e-9: return cost / got, u
        if u == s + 2 and got > 0 and got < N: pass
    return None, None
def hold(c):
    px = c["px"][KEY]; return N * ((1 - px) if c["won"] else -px) - N * fee(px)
base = [hold(c) for c, _, _ in paths]
lost = sum(not c["won"] for c, _, _ in paths)
print(f"=== {int(N)} shares, {len(paths)} entries, 1-9 Nov. Hold to the end: lost {lost}, total {sum(base):+.1f}$, worst entry {min(base):+.1f}$, "
      f"worst day {min(collections.Counter({}).values() or [0]):+.0f}".replace(", worst day +0", ""))
def daymin(xs):
    d = collections.defaultdict(float)
    for (c, _, _), x in zip(paths, xs): d[c["ws"] // 86400] += x
    return min(d.values())
print(f"    hold: worst day {daymin(base):+.1f}$")
for X in (0.80, 0.70, 0.60, 0.50, 0.40):
    for last in (290,):
        res, n_stop, saved, false_, noexit, exits = [], 0, 0, 0, 0, []
        for (c, pp, hit), b in zip(paths, base):
            r = b
            for s in range(c["s"] + 2, last + 1):            # our fill is ~1 s after the signal
                if pp.get(s, 1.0) <= X:
                    ep, u = exit_px(hit, s)
                    if ep is None: noexit += 1; break
                    px = c["px"][KEY]
                    r = N * (ep - px) - N * fee(px) - N * fee(ep)
                    n_stop += 1; exits.append(ep)
                    if c["won"]: false_ += 1
                    else: saved += 1
                    break
            res.append(r)
        print(f"  sell at p <= {X:.2f}: stopped {n_stop:3d} (losers cut {saved:2d} of {lost}, winners sold {false_:2d}), no bid on the tape {noexit:2d}, "
              f"avg exit price {np.mean(exits) if exits else float('nan'):.2f} | total {sum(res):+7.1f}$ ({sum(res) - sum(base):+6.1f} vs hold), "
              f"worst entry {min(res):+6.1f}$, worst day {daymin(res):+6.1f}$")
