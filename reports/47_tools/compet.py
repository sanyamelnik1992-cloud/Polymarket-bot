"""What do the selective last-minute takers do? For each chosen wallet: its first taker buy per window in seconds 240-299
at 0.90-0.98 -> our model state at that second (Binance as Chainlink): p, expected distance, Binance moves.
How many of those entries would the L rule (theta, dist >= 0.5, adverse <= 1/2 bp) have taken? usage: compet.py ASSET WINDOWS BINANCE PFX..."""
import collections, json, math, os, sys
import numpy as np
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.lockmodel import Tables, ClSigma, lock_state, prob_up, expected_dist_bp
asset, wfile, bfile, pfx = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
tables = Tables(os.path.join(ROOT, "config", "lock_tables.npz"))
bn = {int(k): v for k, v in json.load(open(bfile)).items()}
t0, t1 = min(bn), max(bn)
arr = np.full(t1 - t0 + 1, np.nan)
for k, v in bn.items(): arr[k - t0] = v
for i in range(1, len(arr)):
    if np.isnan(arr[i]): arr[i] = arr[i - 1]
cs = ClSigma(); sigarr = np.full(len(arr), np.nan)
for i in range(len(arr)): cs.on_print(t0 + i, arr[i]); sigarr[i] = cs.sigma() or np.nan
P = lambda t: float(arr[t - t0])
def match(w):
    for p in pfx:
        a, b = p.split("…")
        if w[2:2 + len(a)] == a and w.endswith(b): return p
R = collections.defaultdict(list)
for line in open(wfile):
    w = json.loads(line); ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down") or ws - 70 < t0 or ws + 300 > t1: continue
    K = np.mean([P(s) for s in range(ws - 62, ws - 2)])
    seen = set(); tot = collections.defaultdict(float)
    for (t, wal, side, out, size, p, tk) in w["trades"]:
        k = match(wal)
        if not k or not tk or side != "BUY" or not (240 <= t - ws < 299) or not (0.90 <= p <= 0.98): continue
        tot[k] += size
        if k in seen: continue
        seen.add(k)
        s = t - ws; now = ws + s; sg = sigarr[now - 1 - t0]
        if not np.isfinite(sg): continue
        cl = {u: P(u) for u in range(ws + 238, now)}
        st = lock_state(cl, now - 1, P(now - 1), ws, K, float(now), P(now), P(now - 1))
        p_nc = prob_up(tables, asset, st["A_nc"], st["m_nc"], st["S_nc"], K, sg)
        tok_up = out == "Up"
        sgn = 1.0 if tok_up else -1.0; pm = p_nc if tok_up else 1 - p_nc
        dist = sgn * expected_dist_bp(st["A_nc"], st["m_nc"], st["S_nc"], K); spot = sgn * (st["S_nc"] / K - 1) * 1e4
        mv1 = math.log(P(now) / P(now - 1)) * 1e4 * sgn; mv3 = math.log(P(now) / P(now - 3)) * 1e4 * sgn
        R[k].append(dict(s=s, price=p, won=(out == win), pm=pm, dist=dist, spot=spot, mv1=mv1, mv3=mv3, sig=sg * 1e4))
    for k, v in tot.items(): R[k][-1]["size"] = v
if "--losses" in sys.argv[0:] or True:
    print("проигрышные входы (все выбранные кошельки):")
    for k in pfx:
        for x in R[k]:
            if not x["won"]:
                print(f"   {k}: сек {x['s']} цена {x['price']:.2f} p={x['pm']:.3f} дист {x['dist']:.2f} бп спот {x['spot']:.2f} бп Binance за 1с {x['mv1']:+.2f} за 3с {x['mv3']:+.2f} σ {x['sig']:.2f}")
for k in pfx:
    r = R[k]
    if not r: print(f"{k}: нет входов"); continue
    n = len(r); won = sum(x["won"] for x in r)
    def pas(th): return sum(1 for x in r if x["pm"] >= th and x["dist"] >= 0.5 and x["spot"] >= 0 and x["mv1"] >= -1 and x["mv3"] >= -2)
    q = lambda key, f: sorted(x[key] for x in r)[min(n - 1, int(f * n))]
    print(f"{k}: окон {n}, выиграл {won}/{n}, акций в окне (медиана) {sorted(x.get('size', 0) for x in r)[n//2]:.0f}, цена {np.median([x['price'] for x in r]):.3f}, "
          f"секунда {np.median([x['s'] for x in r]):.0f}")
    print(f"      p модели на входе: p10 {q('pm',.1):.3f} p50 {q('pm',.5):.4f} p90 {q('pm',.9):.4f};  дистанция бп: p10 {q('dist',.1):.2f} p50 {q('dist',.5):.2f}; "
          f"σ бп/√с {np.median([x['sig'] for x in r]):.3f}")
    print(f"      прошли бы правило L при θ=0.99: {pas(.99)}/{n} ({pas(.99)/n:.0%}); θ=0.97: {pas(.97)}/{n}; θ=0.95: {pas(.95)}/{n}; θ=0.90: {pas(.90)}/{n}; "
          f"движение Binance против ≥1бп/с: {sum(1 for x in r if x['mv1'] < -1)}/{n}, против ≥2бп/3с: {sum(1 for x in r if x['mv3'] < -2)}/{n}")

allr = [x for k in pfx for x in R[k]]
print("\nвсе выбранные кошельки вместе: входов", len(allr))
for lo, hi in [(0, .8), (.8, .9), (.9, .95), (.95, .99), (.99, 1.01)]:
    g = [x for x in allr if lo <= x["pm"] < hi]
    if g: print(f"   p модели в [{lo:.2f},{hi:.2f}): входов {len(g):3d}, выиграло {sum(x['won'] for x in g)}/{len(g)} = {sum(x['won'] for x in g)/len(g):.1%}, ср. цена {sum(x['price'] for x in g)/len(g):.3f}")
