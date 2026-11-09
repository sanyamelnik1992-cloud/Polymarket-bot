"""Where does the market lag a TWAP-aware model? At fixed seconds t of every window: market Up price (volume-weighted taker
trades within +-3 s) vs model q_t (Gaussian, Binance-proxy strike K, EWMA sigma, exact closing-TWAP structure).
Regression  outcome ~ market + (q - market): a positive weight on (q - market) means the market lags the model.
Also realized edge of 'buy the side the model likes' when |q - market| >= 5c, after spread-free taker fee.
usage: edgemap.py WINDOWS.jsonl BINANCE.json"""
import json, math, sys
import numpy as np
wins = [json.loads(l) for l in open(sys.argv[1])]
bn = {int(k): v for k, v in json.load(open(sys.argv[2])).items()}
t0, t1 = min(bn), max(bn)
arr = np.full(t1 - t0 + 1, np.nan)
for k, v in bn.items(): arr[k - t0] = v
for i in range(1, len(arr)):
    if np.isnan(arr[i]): arr[i] = arr[i - 1]
P = lambda t: arr[t - t0]
lr = np.diff(np.log(arr)); lam = 0.5 ** (1 / 900.0); v = np.var(lr[:3600]); sig = np.zeros(len(lr))
for i, r in enumerate(lr): v = lam * v + (1 - lam) * r * r; sig[i] = math.sqrt(v)
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
def model(ws, K, t, s):
    St = P(ws + t)
    if t < 238:
        sd = s * math.sqrt((238 - t) + 20); mu = St / K - 1
    else:
        n = t - 238 + 1; A = sum(P(ws + u) for u in range(238, t + 1)); m = 60 - n
        EC = (A + m * St) / 60; mu = EC / K - 1
        sd = s * math.sqrt(m * (m + 1) * (2 * m + 1) / 6.0) / 60 if m > 0 else 1e-9
    return Phi(mu / max(sd, 1e-9))
fee = lambda p: 0.07 * p * (1 - p)
TS = [20, 40, 60, 90, 120, 150, 180, 210, 232]
print(f"{'t':>4} {'окон':>4} {'вес (q−рынок)':>16} {'t-стат':>6} | реализованный эдж покупки стороны, которую любит модель, при |q−рынок| ≥ 5¢ (за вычетом комиссии, без спреда)")
for t in TS:
    X = []; E = []
    for w in wins:
        ws, win = w["ws"], w["winner"]
        if win not in ("Up", "Down") or ws - 62 < t0 or ws + 300 > t1: continue
        K = np.mean([P(s) for s in range(ws - 62, ws - 2)]); s = sig[ws - t0 - 1]
        num = den = 0.0
        for (tt, wal, side, out, size, p, tk) in w["trades"]:
            if tk and t - 3 <= tt - ws <= t + 3:
                num += (p if out == "Up" else 1 - p) * size; den += size
        if den < 20: continue
        mk = num / den; q = model(ws, K, t, s); o = 1.0 if win == "Up" else 0.0
        X.append((mk, q, o))
        if q - mk >= 0.05: E.append((o - mk - fee(mk), mk, q - mk))               # buy Up at ~market
        elif mk - q >= 0.05: E.append(((1 - o) - (1 - mk) - fee(1 - mk), 1 - mk, mk - q))   # buy Down
    a = np.array(X); mk, q, o = a[:, 0], a[:, 1], a[:, 2]
    D = np.column_stack([np.ones(len(a)), mk, q - mk]); b, *_ = np.linalg.lstsq(D, o, rcond=None)
    r = o - D @ b; se = np.sqrt(np.diag(np.linalg.inv(D.T @ D) * r.var()))
    e = np.array(E) if E else np.zeros((0, 3))
    tail = f"n={len(e):3d} среднее {100*e[:,0].mean():+5.1f}¢ (±{100*e[:,0].std()/math.sqrt(len(e)):.1f}), ср. q−рынок {100*e[:,2].mean():.1f}¢" if len(e) > 5 else "мало"
    print(f"{t:4d} {len(a):4d} {b[2]:+8.2f}±{se[2]:.2f} {b[2]/se[2]:+6.1f} | {tail}")
