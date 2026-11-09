"""Start-lead test. K = mean of the 60 prices in [T-62, T-3] (Binance 1 s proxy for the Chainlink strike), S_T = price at T.
d0 = (S_T/K - 1) in bp = how far the average LAGS the spot at the window start. If the market ignores K and anchors at S_T,
the outcome should depend on d0 beyond the market price in the first seconds.
usage: startlead.py WINDOWS.jsonl BINANCE.json [T_SEC=10]"""
import json, math, sys
import numpy as np
wins = [json.loads(l) for l in open(sys.argv[1])]
bn = {int(k): v for k, v in json.load(open(sys.argv[2])).items()}
tsec = int(sys.argv[3]) if len(sys.argv) > 3 else 10
t0, t1 = min(bn), max(bn)
arr = np.full(t1 - t0 + 1, np.nan)
for k, v in bn.items(): arr[k - t0] = v
for i in range(1, len(arr)):
    if np.isnan(arr[i]): arr[i] = arr[i - 1]
def P(t): return arr[t - t0]
lr = np.diff(np.log(arr)); lam = 0.5 ** (1 / 900.0)
var = np.zeros(len(lr)); v = np.var(lr[:3600]) if len(lr) > 3600 else 1e-8
for i, r in enumerate(lr):
    v = lam * v + (1 - lam) * r * r; var[i] = v
sig = np.sqrt(var)                                  # per-second std AFTER return i
def Phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
rows = []
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down") or ws - 62 < t0 or ws + 300 > t1: continue
    K = np.mean([P(s) for s in range(ws - 62, ws - 2)])
    ST = P(ws); d0 = (ST / K - 1) * 1e4
    C = np.mean([P(s) for s in range(ws + 238, ws + 298)])
    proxy_up = C >= K
    s = sig[ws - t0 - 1]
    # market Up-equivalent price in rel [tsec-3, tsec+3]: volume-weighted taker trades
    num = den = 0.0
    for (t, wal, side, out, size, p, tk) in w["trades"]:
        if tk and tsec - 3 <= t - ws <= tsec + 3:
            pu = p if out == "Up" else 1 - p
            num += pu * size; den += size
    if den < 20: continue
    St = P(ws + tsec)
    q = Phi(((St / K - 1)) / (s * math.sqrt((238 - tsec) + 20)))
    rows.append((ws, d0, num / den, 1.0 if win == "Up" else 0.0, proxy_up == (win == "Up"), q, s * 1e4))
a = np.array([r[1:] for r in rows], dtype=float)
d0, mk, out, ok, q, sg = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
print(f"окон {len(a)}; прокси исхода по Binance-TWAP совпал с реальным в {ok.mean():.1%}; медианная σ {np.median(sg):.3f} бп/√с; |d0| медиана {np.median(abs(d0)):.2f} бп, p90 {np.percentile(abs(d0),90):.2f} бп")
print(f"рынок через {tsec} с: средняя цена Up {mk.mean():.3f}; корреляция цены рынка с d0: {np.corrcoef(mk, d0)[0,1]:+.2f}; "
      f"корреляция исхода с d0: {np.corrcoef(out, d0)[0,1]:+.2f}; с ценой рынка: {np.corrcoef(out, mk)[0,1]:+.2f}; с моделью q: {np.corrcoef(out, q)[0,1]:+.2f}")
# logistic-ish check: linear probability model out ~ mk + d0 (d0 in units of 1 bp)
X = np.column_stack([np.ones(len(a)), mk, d0]); beta, *_ = np.linalg.lstsq(X, out, rcond=None)
res = out - X @ beta; cov = np.linalg.inv(X.T @ X) * res.var(); se = np.sqrt(np.diag(cov))
print(f"исход ~ цена рынка + d0:  вес цены {beta[1]:.2f}±{se[1]:.2f}, вес d0 {beta[2]:+.3f}±{se[2]:.3f} на 1 бп  (t={beta[2]/se[2]:+.1f})")
print("\nпо величине форы d0 (в σ-единицах 16σ = типичный разброс исхода):")
z = d0 / (sg * 16)
for lo, hi in [(-9, -0.6), (-0.6, -0.3), (-0.3, -0.1), (-0.1, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 9)]:
    m = (z >= lo) & (z < hi)
    if m.sum() < 5: continue
    print(f"  d0/16σ в [{lo:+.1f},{hi:+.1f}): окон {m.sum():3d}  рынок Up {mk[m].mean():.3f}  модель {q[m].mean():.3f}  выиграл Up {out[m].mean():.3f}  "
          f"выиграл Up − рынок {100*(out[m].mean()-mk[m].mean()):+5.1f}¢  (±{100*math.sqrt(out[m].mean()*(1-out[m].mean())/m.sum()):.1f})")
