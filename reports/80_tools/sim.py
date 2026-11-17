"""Early-sniper replay on public data: Binance 1 s (signal), Gamma (outcomes), public taker trades (a seller at <= cap existed).
For each window: the first second t in [end - x_hi, end - x_lo] with z(t) >= z0 for the leader AND a taker buy of the leader
at <= cap within [t, t + 5 s]; one entry per window, filled at that trade's price. Prints entries a day, lost, pnl per share."""
import json, sys, collections, numpy as np
from zmod import *
W = [w for w in json.load(open("windows.json"))]
MT = json.load(open("mtrades.json"))
DAYS = 8.0
def fee(p): return 0.07 * p * (1 - p)
def run(kind, z0, x_hi, x_lo, cap, pmin=0.90, lead_bp=None, step=2, exclude=None):
    ent = []
    for w in W:
        if w["kind"] != kind or w["slug"] not in MT: continue
        rows = MT[w["slug"]]
        buys = collections.defaultdict(list)       # outcome -> [(t, px)]
        for t, sd, o, sz, px_, wal in rows:
            if sd == "B" and pmin <= px_ <= cap and (exclude is None or wal != exclude): buys[o].append((t, px_))
        if not buys: continue
        for t in range(w["end"] - x_hi, w["end"] - x_lo + 1, step):
            lead, _, _ = z_at(w, t)
            if lead_bp is not None:
                ok = abs(lead) >= lead_bp
            else:
                ok = abs(lead) / (sigma(w["asset"], t, 3600) * np.sqrt(tau(w, t))) >= z0
            if not ok: continue
            side = "Up" if lead > 0 else "Down"
            f = [px_ for tt, px_ in buys.get(side, []) if t <= tt <= t + 5]
            if not f: continue
            p = f[0]
            ent.append(dict(slug=w["slug"], asset=w["asset"], x=w["end"] - t, px=p, won=side == w["win"], t=t))
            break
    n = len(ent); lost = sum(not e["won"] for e in ent)
    if not n: return ent, f"{kind} z>={z0} x[{x_lo},{x_hi}] cap {cap}: no entries"
    pnl = sum((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"]) for e in ent)
    avgpx = sum(e["px"] for e in ent) / n
    return ent, (f"{kind} {'lead>=%.0fbp' % lead_bp if lead_bp else 'z>=%.1f' % z0} x[{x_lo},{x_hi}] cap {cap}: {n} entries ({n/DAYS:.1f}/day), "
                 f"lost {lost} ({100*lost/n:.2f}%), avg px {avgpx:.4f}, pnl/share {100*pnl/n:+.2f}c, $/day at 10 sh {10*pnl/DAYS:+.2f}")
if __name__ == "__main__":
    kind = sys.argv[1]
    for args in json.loads(sys.argv[2]):
        _, s = run(kind, **args); print(s, flush=True)
