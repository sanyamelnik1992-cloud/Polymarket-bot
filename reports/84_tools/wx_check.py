"""First forecast-vs-market check on the recorder's extract (server-reports weather/extract-DATE.csv.gz).
Model v0: final max X = max(observed so far, F + e), F = mean of the three models' day max (C), e ~ Normal(bias, sd(h)),
bias and sd from the OTHER dates (leave-one-date-out), rounding as the market rules (C: round half up; F: C*9/5+32 then round).
Compares log loss of the winning bucket: model vs market (bucket mids normalized to 1), by hours before the end of the day,
and a simple trade: buy YES when model - ask >= EDGE, or NO when (1 - model) - (1 - bid) >= EDGE, held to resolution."""
import csv, glob, gzip, math, sys, collections, statistics as stt
EDGE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
def fee(p): return 0.05 * p * (1 - p)
rows = []
for p in sorted(glob.glob((sys.argv[1] if len(sys.argv) > 1 else "wx") + "/extract-*.csv.gz")):
    rows += list(csv.DictReader(gzip.open(p, "rt")))
ev = collections.defaultdict(list)
for r in rows: ev[(r["slug"], int(r["h"]))].append(r)
def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None
# errors of the forecast mean vs the final observed max (C), per date, by h
final = {}
for (slug, h), bs in ev.items():
    if h == 2 and bs[0]["obs_c"] not in ("", None): final[slug] = f(bs[0]["obs_c"])
def fmean(b):
    xs = [f(b[k]) for k in ("fc_ecmwf_ifs025", "fc_gfs_seamless", "fc_icon_seamless") if f(b[k]) is not None]
    return sum(xs) / len(xs) if xs else None
err = collections.defaultdict(list)          # (date, h) -> [obs final - forecast]
for (slug, h), bs in ev.items():
    F = fmean(bs[0])
    if F is not None and slug in final: err[(bs[0]["date"], h)].append(final[slug] - F)
dates = sorted({d for d, _ in err})
def stats_excl(date, h):
    xs = [e for (d, hh), v in err.items() if d != date and hh == h for e in v]
    if len(xs) < 20: xs = [e for (d, hh), v in err.items() if d != date for e in v]
    return (stt.mean(xs), max(0.5, stt.pstdev(xs))) if len(xs) >= 20 else (0.0, 2.0)
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
def to_unit(c, unit): return c * 9 / 5 + 32 if unit == "F" else c
def bucket_probs(bs, F, obs, mu, sd):
    unit = bs[0]["unit"]
    out = []
    for b in bs:
        lo, hi = f(b["lo"]), f(b["hi"])
        # P(rounded final max in [lo, hi]) with final = max(obs, F + e) in C, compared in the event unit
        def cdf(x_unit):                       # P(final_unit < x_unit)
            x_c = (x_unit - 32) * 5 / 9 if unit == "F" else x_unit
            if obs is not None and obs >= x_c: return 0.0
            return Phi((x_c - F - mu) / sd)
        a = cdf(lo - 0.5) if lo is not None else 0.0
        z = cdf(hi + 0.5) if hi is not None else 1.0
        out.append(max(1e-4, z - a))
    s = sum(out)
    return [x / s for x in out]
res = collections.defaultdict(lambda: dict(n=0, ll_m=0.0, ll_k=0.0, tr=0, cost=0.0, pnl=0.0, win=0))
for (slug, h), bs in ev.items():
    F = fmean(bs[0])
    if F is None or not any(b["won"] == "1" for b in bs): continue
    mids = []
    for b in bs:
        bid, ask = f(b["bid"]), f(b["ask"])
        mids.append((bid + ask) / 2 if bid is not None and ask is not None else (ask if ask is not None else bid))
    if any(m is None for m in mids): continue
    sm = sum(mids)
    if sm <= 0: continue
    mk = [max(1e-4, m / sm) for m in mids]
    mu, sd = stats_excl(bs[0]["date"], h)
    obs = f(bs[0]["obs_c"])
    pm = bucket_probs(bs, F, obs, mu, sd)
    wi = [i for i, b in enumerate(bs) if b["won"] == "1"][0]
    g = res[h]; g["n"] += 1; g["ll_m"] += -math.log(pm[wi]); g["ll_k"] += -math.log(mk[wi])
    for i, b in enumerate(bs):
        bid, ask = f(b["bid"]), f(b["ask"])
        won = b["won"] == "1"
        if ask is not None and 0.02 <= ask <= 0.98 and pm[i] - ask - fee(ask) >= EDGE:
            g["tr"] += 1; g["cost"] += ask; g["pnl"] += (1.0 if won else 0.0) - ask - fee(ask); g["win"] += won
        if bid is not None and 0.02 <= bid <= 0.98 and (1 - pm[i]) - (1 - bid) - fee(1 - bid) >= EDGE:
            c = 1 - bid
            g["tr"] += 1; g["cost"] += c; g["pnl"] += (0.0 if won else 1.0) - c - fee(c); g["win"] += (not won)
print(f"dates: {dates}; events x times: {sum(g['n'] for g in res.values())}; EDGE {EDGE}")
print(" h   events  logloss model  market   trades  won   cost$/sh  pnl$/sh  ROI")
for h in sorted(res, reverse=True):
    g = res[h]
    print(f"{h:3d} {g['n']:7d}   {g['ll_m']/g['n']:8.3f}  {g['ll_k']/g['n']:8.3f}   {g['tr']:6d} {g['win']:4d}  {g['cost']:8.1f}  {g['pnl']:+7.2f}  {100*g['pnl']/max(g['cost'],1e-9):+5.1f}%")
for h in sorted({h for _, h in err}, reverse=True):
    xs = [e for (d, hh), v in err.items() if hh == h for e in v]
    print(f"forecast error at h={h}: n {len(xs)} mean {stt.mean(xs):+.2f} C sd {stt.pstdev(xs):.2f} C")
