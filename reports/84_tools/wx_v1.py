"""Weather model v1 vs the market (leave-one-date-out): station bias (shrunk to the global one), sd by horizon widened by the
models' spread, the day max floored by the observed max; log loss of the winning bucket for the market, v0, v1 and
blends market^(1-w) * v1^w; and trades at EDGE."""
import csv, glob, gzip, math, sys, collections, statistics as stt
SRC = sys.argv[1]; EDGE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
MODELS = ("fc_ecmwf_ifs025", "fc_gfs_seamless", "fc_icon_seamless")
def fee(p): return 0.05 * p * (1 - p)
def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None
rows = []
for p in sorted(glob.glob(SRC + "/extract-*.csv.gz")):
    rows += list(csv.DictReader(gzip.open(p, "rt")))
ev = collections.defaultdict(list)
for r in rows: ev[(r["slug"], int(r["h"]))].append(r)
final = {s: f(bs[0]["obs_c"]) for (s, h), bs in ev.items() if h == 2 and f(bs[0]["obs_c"]) is not None}
def fc(b):
    xs = [f(b[k]) for k in MODELS if f(b[k]) is not None]
    return (sum(xs) / len(xs), (max(xs) - min(xs)) if len(xs) > 1 else 0.0) if xs else (None, None)
E = []                                                   # (date, station, h, err, spread)
for (s, h), bs in ev.items():
    F, sp = fc(bs[0])
    if F is not None and s in final:
        E.append((bs[0]["date"], bs[0]["station"], h, final[s] - F, sp))
def Phi(z): return 0.5 * math.erfc(-z / math.sqrt(2))
def params(date, station, h):
    oth = [e for e in E if e[0] != date]
    gh = [e for e in oth if e[2] == h] or oth
    mu_g = stt.mean(x[3] for x in gh); sd_g = stt.pstdev(x[3] for x in gh)
    st = [x[3] for x in gh if x[1] == station]
    k = 3.0
    mu = (sum(st) + k * mu_g) / (len(st) + k)            # shrunk station bias
    res = [x[3] - mu_g for x in gh]
    return mu, mu_g, max(0.5, sd_g)
def probs(bs, F, obs, mu, sd):
    unit = bs[0]["unit"]; out = []
    for b in bs:
        lo, hi = f(b["lo"]), f(b["hi"])
        def cdf(x_unit):
            x_c = (x_unit - 32) * 5 / 9 if unit == "F" else x_unit
            if obs is not None and obs >= x_c: return 0.0
            return Phi((x_c - F - mu) / sd)
        a = cdf(lo - 0.5) if lo is not None else 0.0
        z = cdf(hi + 0.5) if hi is not None else 1.0
        out.append(max(1e-4, z - a))
    s = sum(out); return [x / s for x in out]
W = (0.0, 0.1, 0.2, 0.3, 0.5)
res = collections.defaultdict(lambda: collections.defaultdict(float))
for (s, h), bs in sorted(ev.items()):
    F, sp = fc(bs[0])
    if F is None or s not in final or not any(b["won"] == "1" for b in bs): continue
    mids = []
    for b in bs:
        bid, ask = f(b["bid"]), f(b["ask"])
        mids.append((bid + ask) / 2 if bid is not None and ask is not None else (ask if ask is not None else bid))
    if any(m is None for m in mids) or sum(mids) <= 0: continue
    mk = [max(1e-4, m / sum(mids)) for m in mids]
    date, station = bs[0]["date"], bs[0]["station"]
    mu, mu_g, sd = params(date, station, h)
    obs = f(bs[0]["obs_c"])
    p0 = probs(bs, F, obs, mu_g, sd)
    sd1 = math.sqrt(sd ** 2 + 0.25 * sp ** 2)
    p1 = probs(bs, F, obs, mu, sd1)
    wi = [i for i, b in enumerate(bs) if b["won"] == "1"][0]
    g = res[h]; g["n"] += 1; g["mk"] += -math.log(mk[wi]); g["v0"] += -math.log(p0[wi]); g["v1"] += -math.log(p1[wi])
    for w in W:
        bl = [m ** (1 - w) * q ** w for m, q in zip(mk, p1)]; sb = sum(bl)
        g[f"b{w}"] += -math.log(bl[wi] / sb)
    for i, b in enumerate(bs):
        bid, ask = f(b["bid"]), f(b["ask"]); won = b["won"] == "1"
        if ask is not None and 0.02 <= ask <= 0.98 and p1[i] - ask - fee(ask) >= EDGE:
            g["tr"] += 1; g["cost"] += ask + fee(ask); g["pnl"] += (1.0 if won else 0.0) - ask - fee(ask)
        if bid is not None and 0.02 <= bid <= 0.98 and (1 - p1[i]) - (1 - bid) - fee(1 - bid) >= EDGE:
            c = 1 - bid; g["tr"] += 1; g["cost"] += c + fee(c); g["pnl"] += (0.0 if won else 1.0) - c - fee(c)
print(f"events with outcome: {len(final)}; EDGE {EDGE}")
print("  h    n   market     v0     v1  " + "  ".join(f"blend{w}" for w in W[1:]) + "   v1 trades  cost   pnl   ROI")
for h in sorted(res, reverse=True):
    g = res[h]; n = int(g["n"])
    print(f"{h:3d} {n:4d}  {g['mk']/n:7.3f} {g['v0']/n:6.3f} {g['v1']/n:6.3f}  " + "  ".join(f"{g[f'b{w}']/n:7.3f}" for w in W[1:]) +
          f"   {int(g['tr']):5d} {g['cost']:6.1f} {g['pnl']:+6.2f} {100*g['pnl']/max(g['cost'],1e-9):+5.1f}%")
