"""Sampled CLOB books of the closing minute (book_sampler.py, ~3 polls a second) + the lock model on Binance 1 s closes:
how long does an ask within the L3 tier limit live, how wide is the spread of the decided side, how much size is at the best bid.
usage: an_books.py BOOKS.jsonl BINANCE_DIR"""
import os
import collections, json, math, sys
import numpy as np
import requests
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.lockmodel import Tables, ClSigma, lock_state, prob_up, expected_dist_bp  # noqa: E402
tables = Tables(os.path.join(ROOT, "config", "lock_tables.npz"))
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
TIERS = ((.99, .98), (.97, .95), (.95, .93), (.90, .91))
def lim_for(p):
    best = None
    for pm, am in TIERS:
        if p >= pm and (best is None or am > best): best = am
    return best
px, sg = {}, {}
for a, s in SYM.items():
    bn = {int(k): v for k, v in json.load(open(f"{sys.argv[2]}/bn_{s}.json")).items()}
    t0, t1 = min(bn), max(bn)
    arr = np.full(t1 - t0 + 1, np.nan)
    for k, v in bn.items(): arr[k - t0] = v
    for i in range(1, len(arr)):
        if np.isnan(arr[i]): arr[i] = arr[i - 1]
    cs = ClSigma(); sig = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        cs.on_print(t0 + i, arr[i]); sig[i] = cs.sigma() or np.nan
    px[a], sg[a] = (t0, arr), sig
def P(a, t): return float(px[a][1][t - px[a][0]])
def state(a, ws, s):
    """(side 'U'/'D', p) of the production state rule at integer second s, or None."""
    now = ws + s
    sgm = sg[a][now - 1 - px[a][0]]
    if not np.isfinite(sgm): return None
    K = float(np.mean([P(a, u) for u in range(ws - 62, ws - 2)]))
    cl = {u: P(a, u) for u in range(ws + 238, now)}
    st = lock_state(cl, now - 1, P(a, now - 1), ws, K, float(now), P(a, now), P(a, now - 1))
    p_nc = prob_up(tables, a, st["A_nc"], st["m_nc"], st["S_nc"], K, sgm)
    up = p_nc >= 0.5; sgn = 1.0 if up else -1.0; p = p_nc if up else 1 - p_nc
    dist = sgn * expected_dist_bp(st["A_nc"], st["m_nc"], st["S_nc"], K); spot = sgn * (st["S_nc"] / K - 1) * 1e4
    mv1 = math.log(P(a, now) / P(a, now - 1)) * 1e4; mv3 = math.log(P(a, now) / P(a, now - 3)) * 1e4
    if dist < 0.5 or spot < 0 or -sgn * mv1 > 1.0 or -sgn * mv3 > 2.0 or abs(mv1) > 5.0: return None
    return ("U" if up else "D"), p
polls = collections.defaultdict(list)          # (ws, asset) -> [(t, book U, book D)]
for line in open(sys.argv[1]):
    r = json.loads(line)
    for a, b in r["books"].items():
        if "U" in b and "D" in b:
            polls[(r["ws"], a)].append((r["t"] + r["dt"] / 2, b["U"], b["D"]))
cache = {}
rows = []          # one per poll with a valid state and p >= 0.90
for (ws, a), ps in sorted(polls.items()):
    for (t, bu, bd) in ps:
        s = int(t - ws)
        if not (240 <= s <= 296): continue
        key = (ws, a, s)
        if key not in cache: cache[key] = state(a, ws, s)
        st = cache[key]
        if not st or st[1] < 0.90: continue
        side, p = st
        b = bu if side == "U" else bd
        if not b["asks"] or not b["bids"]: continue
        ask, asz = b["asks"][0]; bid, bsz = b["bids"][0]
        lim = lim_for(p)
        rows.append(dict(ws=ws, a=a, t=t, s=s, p=p, ask=ask, asz=asz, bid=bid, bsz=bsz, lim=lim, sig=(0.50 <= ask <= lim), sig3=(0.90 <= ask <= lim)))
print(f"polls with a valid state and p >= 0.90: {len(rows)} in {len({(r['ws'], r['a']) for r in rows})} window-coins of {len(polls)}")
for a in SYM:
    v = [r for r in rows if r["a"] == a]
    if not v: continue
    sp = sorted(round(100 * (r["ask"] - r["bid"])) for r in v)
    bs = sorted(r["bsz"] for r in v); az = sorted(r["asz"] for r in v)
    c = collections.Counter(sp)
    print(f"  {a}: polls {len(v):4d} | spread of the decided side, cents: 1c {100*c[1]/len(v):3.0f}%  2c {100*c[2]/len(v):3.0f}%  3c+ {100*sum(n for k, n in c.items() if k >= 3)/len(v):3.0f}%"
          f" | size at best bid: median {bs[len(bs)//2]:.0f}, p25 {bs[len(bs)//4]:.0f} | size at best ask: median {az[len(az)//2]:.0f}"
          f" | ask within the L3 limit {100*sum(r['sig3'] for r in v)/len(v):.0f}% of polls, within L4 {100*sum(r['sig'] for r in v)/len(v):.0f}%")
# lifetime of a taker signal: from the first poll where the ask is within the limit, is it still within the limit 0.3 / 0.6 / 1 / 2 s later?
by = collections.defaultdict(list)
for r in rows: by[(r["ws"], r["a"])].append(r)
allp = {k: sorted(v, key=lambda r: r[0]) for k, v in polls.items()}
def ask_at(k, side, t):
    best = None
    for (tt, bu, bd) in allp[k]:
        if tt > t: break
        b = bu if side == "U" else bd
        best = b["asks"][0] if b["asks"] else None
    return best
for name, key in (("L3 signal (ask 0.90..limit)", "sig3"), ("L4 signal (ask 0.50..limit)", "sig")):
    ev = []
    for k, v in by.items():
        v.sort(key=lambda r: r["t"])
        prev = False
        for r in v:
            if r[key] and not prev:
                ev.append((k, r))
            prev = r[key]
    print(f"\n{name}: {len(ev)} signal starts in {len({k for k, _ in ev})} window-coins")
    for dt in (0.3, 0.6, 1.0, 2.0, 3.0):
        still = gone = better = 0
        for k, r in ev:
            side = cache[(r["ws"], r["a"], r["s"])][0]
            a2 = ask_at(k, side, r["t"] + dt)
            if a2 is None or a2[0] > r["lim"] + 1e-9: gone += 1
            else:
                still += 1; better += a2[0] < r["ask"] - 1e-9
        n = still + gone
        print(f"   {dt:.1f} s later: ask still within the limit {100*still/n:4.0f}%  (of those, cheaper than at the signal: {100*better/max(still,1):3.0f}%)   gone {100*gone/n:4.0f}%")
    szs = sorted(r["asz"] for _, r in ev)
    print(f"   size at the best ask when the signal starts: median {szs[len(szs)//2]:.0f}, p25 {szs[len(szs)//4]:.0f}, p75 {szs[3*len(szs)//4]:.0f} shares")
