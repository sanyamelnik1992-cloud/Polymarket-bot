"""Market-conditioned calibration: for each window, the first moment where the model is in a tier AND the ask is in a band.
When the market agrees (ask high), how often does that tier lose; when the market doubts (ask <= tier limit), how often?
EV per share at that ask = win rate - ask - fee. Two halves of the period for consistency."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "59_tools"))
import base
from base import *
from pmbot.fees import fee_per_share
PER = ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c", "d1111", "d1111b"]
base.PER[:] = PER
g = load_all()
G, ok, lim = live_rule(g, tiers=L3[:3]); G = G[ok].copy()
G["half"] = np.where(G.period.isin(PER[:5]), "7-9 Nov", "10-11 Nov")
bands = {"1 p>=.99": (0.99, 1.01, [(0.90, 0.98, "ask .90-.98 (we buy)"), (0.985, 1.0, "ask >= .985")]),
         "2 .97-.99": (0.97, 0.99, [(0.90, 0.95, "ask .90-.95 (we buy)"), (0.97, 1.0, "ask >= .97")]),
         "3 .95-.97": (0.95, 0.97, [(0.90, 0.93, "ask .90-.93 (we buy)"), (0.97, 1.0, "ask >= .97")])}
for tname, (lo, hi, abands) in bands.items():
    print(f"== tier {tname}")
    for alo, ahi, aname in abands:
        s = G[(G.p >= lo) & (G.p < hi) & (G.ask >= alo - 1e-9) & (G.ask <= ahi + 1e-9)].sort_values("t").groupby(["asset", "T"]).head(1)
        if not len(s): continue
        ev = (s.won.astype(float) - s.ask - s.ask.map(fee_per_share)).mean() * 100
        be = (1 - s.ask - s.ask.map(fee_per_share)).mean() * 100
        print(f"   {aname:22s} windows {len(s):4d} lost {int((~s.won).sum()):3d} ({100*(~s.won).mean():4.1f}%)  model {100*(1-s.p).mean():.1f}%"
              f"  break-even {100-be:4.1f}%  EV {ev:+5.2f} c/share  mean ask {s.ask.mean():.3f}")
        for h, w in s.groupby("half"):
            evh = (w.won.astype(float) - w.ask - w.ask.map(fee_per_share)).mean() * 100
            print(f"       {h:10s} windows {len(w):4d} lost {int((~w.won).sum()):3d} ({100*(~w.won).mean():4.1f}%) EV {evh:+5.2f} c/share")
