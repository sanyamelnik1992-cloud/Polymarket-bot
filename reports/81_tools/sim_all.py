"""Report 80's early-sniper replay (sim.py) on any date range of 1-16 Nov: Binance 1 s (signal, strike), Gamma (outcome),
public taker trades (fill = a buy of the leader at [pmin, cap] within [t, t + 5 s], at that trade's price).
Same logic as reports/80_tools/sim.py + zmod.py; only the data files and the date filter differ."""
import json, sys, collections, calendar, time
import numpy as np
B = np.load("bn1s_all.npz"); T0 = int(B["t0"])
PX = {a: B[s] for a, s in (("btc", "BTCUSDT"), ("eth", "ETHUSDT"), ("sol", "SOLUSDT"), ("xrp", "XRPUSDT"))}
LR = {a: np.diff(np.log(p), prepend=np.log(p[0])) * 1e4 for a, p in PX.items()}
CS = {a: np.concatenate([[0.0], np.cumsum(r * r)]) for a, r in LR.items()}
N = len(PX["btc"]); END_OK = T0 + 17 * 86400 - 1          # Binance archives end 16 Nov 23:59:59
W = json.load(open("windows_all.json")); MT = json.load(open("mtrades_all.json"))
def px(a, t): return PX[a][int(t) - T0]
def strike(w): return PX[w["asset"]][w["start"] - 62 - T0: w["start"] - 2 - T0].mean()
def sigma(a, t, n):
    i = int(t) - T0
    return float(np.sqrt((CS[a][i] - CS[a][i - n]) / n))
def tau(w, t):
    ws = w["end"] - 62
    return (ws - t) + 59 / 3.0
def fee(p): return 0.07 * p * (1 - p)
def run(kind, z0, x_hi, x_lo, cap, pmin, d0, d1, step=2, sig_n=3600):
    ent = []
    for w in W:
        if w["kind"] != kind or w["slug"] not in MT or not (d0 <= w["start"] < d1) or w["end"] > END_OK: continue
        if w["start"] - 62 - T0 - sig_n < 0: continue
        buys = collections.defaultdict(list)
        for t, sd, o, sz, p_, wal in MT[w["slug"]]:
            if sd == "B" and pmin <= p_ <= cap: buys[o].append((t, p_))
        if not buys: continue
        K = strike(w)
        for t in range(w["end"] - x_hi, w["end"] - x_lo + 1, step):
            lead = np.log(px(w["asset"], t) / K) * 1e4
            z = abs(lead) / (sigma(w["asset"], t, sig_n) * np.sqrt(tau(w, t)) + 1e-9)
            if z < z0: continue
            side = "Up" if lead > 0 else "Down"
            f = [p_ for tt, p_ in buys.get(side, []) if t <= tt <= t + 5]
            if not f: continue
            ent.append(dict(slug=w["slug"], asset=w["asset"], x=w["end"] - t, px=f[0], won=side == w["win"], t=t, z=z))
            break
    return ent
def summary(ent, days):
    n = len(ent); lost = sum(not e["won"] for e in ent)
    if not n: return "no entries"
    pnl = sum((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"]) for e in ent)
    return f"{n} entries ({n/days:.0f}/day), lost {lost} ({100*lost/n:.2f}%), avg px {np.mean([e['px'] for e in ent]):.4f}, pnl/share {100*pnl/n:+.2f}c, $/day at 10 sh {10*pnl/days:+.2f}"
def by_day(ent):
    by = collections.defaultdict(list)
    for e in ent: by[time.strftime("%m-%d", time.gmtime(e["t"]))].append(e)
    return " ".join(f"{d}:{len(v)}/{sum(not e['won'] for e in v)}/{10*sum((1.0 if e['won'] else 0.0) - e['px'] - fee(e['px']) for e in v):+.1f}" for d, v in sorted(by.items()))
if __name__ == "__main__":
    D = lambda d: calendar.timegm((2026, 11, d, 0, 0, 0))
    periods = [("1-8 Nov (OOS)", D(1), D(9)), ("9-16 Nov (report 80)", D(9), D(17))]
    rules = [("5m z>=3 x120-62 0.985-0.99", "5m", 3.0, 120, 62, 0.99, 0.985),
             ("5m z>=3.5 x120-62 0.985-0.99", "5m", 3.5, 120, 62, 0.99, 0.985),
             ("5m z>=3 x180-62 0.985-0.99", "5m", 3.0, 180, 62, 0.99, 0.985),
             ("15m z>=3 x300-120 0.985-0.99", "15m", 3.0, 300, 120, 0.99, 0.985),
             ("15m z>=3.5 x300-120 0.985-0.99", "15m", 3.5, 300, 120, 0.99, 0.985),
             ("5m his rule: lead 17bp-ish -> z>=0 x130-78 0.97-0.99", None, None, None, None, None, None)]
    for pname, d0, d1 in periods:
        days = (d1 - d0) / 86400
        print(f"\n=== {pname}")
        for name, kind, z0, xh, xl, cap, pmin in rules:
            if kind is None: continue
            ent = run(kind, z0, xh, xl, cap, pmin, d0, d1)
            print(f"  {name:34} {summary(ent, days)}")
            print(f"      by day (n/lost/$ at 10 sh): {by_day(ent)}")
