"""Rule variants on all 9 periods (fills modeled: book 300 ms later, retry 1 s), per period, + the entry-condition sample."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "59_tools"))
import base
from base import *
from sim import run_delay
PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
base.PER[:] = PER
g = load_all()
T12 = [[0.99, 0.98], [0.97, 0.95]]
V = {
    "A live rule (3 tiers)": dict(tiers=L3[:3]),
    "B + BTC only p>=0.97": dict(tiers=L3[:3], extra=lambda G: (G.asset != "btc") | (G.p >= 0.97)),
    "C tiers 1-2 only (p>=0.97)": dict(tiers=T12),
    "D tier 3 only from s260": dict(tiers=L3[:3], extra=lambda G: (G.p >= 0.97) | (G.rel >= 260)),
    "E tier 3 only from s255": dict(tiers=L3[:3], extra=lambda G: (G.p >= 0.97) | (G.rel >= 255)),
    "F tier 3 at ask<=0.92": dict(tiers=[[0.99, 0.98], [0.97, 0.95], [0.95, 0.92]]),
    "G tier 3 at ask<=0.91": dict(tiers=[[0.99, 0.98], [0.97, 0.95], [0.95, 0.91]]),
}
out = {}
for nm, kw in V.items():
    G, ok, lim = live_rule(g, **kw)
    e = run(G, ok, lim).sort_values("t"); x = limits(e, 5, 1e9)
    out[nm] = x
    per = [x[x.period == p] for p in PER]
    neg = sum(1 for w in per if w.pnl.sum() < 0)
    worst = min(w.pnl.sum() for w in per)
    print(f"{line(nm, x)} | periods <0: {neg}/9, worst {worst:+.2f}")
    e7 = run_delay(G, ok, lim, exec_ms=600)
    x7 = limits(e7.sort_values("t"), 5, 1e9) if len(e7) else e7
    print(f"   {'(order 600 ms late)':44s} n={len(x7):3d} lost={int((~x7.won).sum()):2d} pnl={x7.pnl.sum():+7.2f}")
print("per period (one entry at a time): n / lost / pnl")
for p in PER:
    print(f"  {p:7s} " + " ".join(f"{nm[:1]}:{len(w):3d}/{int((~w.won).sum())}/{w.pnl.sum():+6.2f}" for nm, x in out.items() for w in [x[x.period == p]]))
