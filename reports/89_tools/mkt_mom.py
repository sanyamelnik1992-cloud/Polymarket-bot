"""All public taker BUYs in BTC 5m windows (r22 data, 18-21 Nov): return at resolution by (second of the window, our fair
value of the side, Binance move toward the side over the last 60 s). Does the market underprice the side Binance moved toward?"""
import json, sys, collections, math
import numpy as np
sys.path.insert(0, ".")
from fairlib import p_up, has, mom, fee
D = sys.argv[1]
W = {w["slug"]: w for w in json.load(open(f"{D}/windows.json")) if w["asset"] == "btc" and w["kind"] == "5m"}
TR = json.load(open(f"{D}/trades.json"))
rows = []
for s, w in W.items():
    for ts, sd, out, n, p, wal in TR.get(s, []):
        if sd != "B" or not (0.02 <= p <= 0.98): continue
        t = int(ts); rel = t - w["start"]
        if not (0 <= rel < 238) or not has("btc", t - 3700): continue
        pu = p_up("btc", w["start"], 300, t, 1)
        if pu is None: continue
        f = pu if out == "Up" else 1 - pu
        sg = 1 if out == "Up" else -1
        won = w["win"] == out
        rows.append((rel, f, sg * mom("btc", t - 1, 60), p, n, won))
A = np.array(rows, dtype=float)
rel, f, m60, p, n, won = A.T
usd = p * n; pnl = n * (won - p) - n * 0.07 * p * (1 - p)
print("taker buys", len(A), "usd", round(usd.sum()), "roi all", round(100 * pnl.sum() / usd.sum(), 2), "%")
def cell(mask):
    u = usd[mask].sum()
    return (mask.sum(), u, 100 * pnl[mask].sum() / u if u else float("nan"), (usd[mask] * won[mask]).sum() / u if u else 0, (usd[mask] * p[mask]).sum() / u if u else 0)
print("\nsecond 120-238, by our fair of the side (rows) and Binance 60 s move toward it (cols): roi % (usd k)")
fe = [0, .2, .4, .5, .6, .7, .8, .9, 1.01]; me = [-99, -10, -5, -2, 0, 2, 5, 10, 99]
print("fair \\ m60  " + " ".join(f"{lo:>5}..{hi:<4}" for lo, hi in zip(me[:-1], me[1:])))
for flo, fhi in zip(fe[:-1], fe[1:]):
    line = f"{flo:.1f}-{fhi:.1f}    "
    for mlo, mhi in zip(me[:-1], me[1:]):
        k = (rel >= 120) & (f >= flo) & (f < fhi) & (m60 >= mlo) & (m60 < mhi)
        c = cell(k)
        line += f" {c[2]:+6.1f}({c[1]/1000:4.0f})" if c[1] > 2000 else "     .     "
    print(line)
print("\nsame for second 30-120")
for flo, fhi in zip(fe[:-1], fe[1:]):
    line = f"{flo:.1f}-{fhi:.1f}    "
    for mlo, mhi in zip(me[:-1], me[1:]):
        k = (rel >= 30) & (rel < 120) & (f >= flo) & (f < fhi) & (m60 >= mlo) & (m60 < mhi)
        c = cell(k)
        line += f" {c[2]:+6.1f}({c[1]/1000:4.0f})" if c[1] > 2000 else "     .     "
    print(line)
print("\ncalibration: our fair vs won share of all buys (usd-weighted), second 30-238")
for flo, fhi in zip(fe[:-1], fe[1:]):
    k = (rel >= 30) & (f >= flo) & (f < fhi)
    c = cell(k)
    print(f"  fair {flo:.1f}-{fhi:.1f}: buys {c[0]:6d} usd {c[1]:9.0f} won {c[3]:.3f} paid {c[4]:.3f} roi {c[2]:+.1f} %")
