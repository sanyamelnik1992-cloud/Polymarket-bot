"""Honest simulation of quoting rules on the public tape (table.npz from tape_table.py).

  python tape_rules.py TABLE.npz [TABLE2.npz ...]

Our bid sits at b = ref - h (on the 0.01 grid) and is the better price: every taker trade at a price <= b fills US at OUR price b
(price priority), so pnl per share = outcome - b (makers pay no fee). Not the trade's price: that overstated the edge by the price
improvement (the first version of this study printed +2.6 c for the symmetric fair - 1 c maker; the honest figure is -1.5 c).
Weights: min(shares, 5) per trade row. The simulated volume is an upper bound (one sweep through several levels is several rows), the
per-share figures are what to compare. ref = the model's fair value, or min(fair, market) where the market is the mean Up price of the
trades of the 5 seconds before (the book mid is not on the tape). fav = the model's fair value of the side >= 0.5.
The symmetric 'min' rule (R0m) is the live maker before 26 Nov: its tape figure for 22-25 Nov (+0.32 c) matches its paper twin on the
server (+0.30 c in 2.5 days)."""
import sys
import numpy as np
from scipy.special import ndtr, ndtri


def load(path):
    T = np.load(path)
    return {k: T[k] for k in T.files}


def evaluate(t, label, hf, hd, ref="fair", rel_lo=20, gate=None, th=0.5, pmin=0.10, pmax=0.90, fav_thr=0.5, kappa=1.0, rel_hi=230):
    base = (t["rel"] >= rel_lo) & (t["rel"] <= rel_hi) & np.isfinite(t["fL"])
    won = t["pnl"] + t["pl"]
    w = np.minimum(t["n"], 5.0)
    pl, mL = t["pl"], t["mL"]
    f = ndtr(kappa * ndtri(np.clip(t["fL"], 1e-6, 1 - 1e-6)))
    r_ = np.where(np.isfinite(mL), np.minimum(f, mL), f) if ref == "min" else f
    fav = t["fL"] >= fav_thr
    b = np.floor((r_ - np.where(fav, hf, hd)) * 100 + 1e-9) / 100
    m = base & (r_ >= pmin) & (r_ <= pmax) & (b >= 0.05) & (pl <= b + 1e-9)
    if gate:
        m &= ~(t[gate] < -th)                          # Binance moved against the side by more than th bp: no bid
    pn = (won - b) * w
    days = np.unique(t["day"])
    byd = " ".join(f"{100 * pn[m & (t['day'] == d)].sum() / max(w[m & (t['day'] == d)].sum(), 1):+5.1f}" for d in days)
    print(f"  {label:50s} {100 * pn[m].sum() / w[m].sum():+6.2f} c/sh  {w[m].sum() / len(days):8.0f} sh/day  by day [{byd}]")


if __name__ == "__main__":
    for name in sys.argv[1:]:
        t = load(name)
        print(name, "days", np.unique(t["day"]))
        evaluate(t, "R0   symmetric fair - 1c", 0.01, 0.01)
        evaluate(t, "R0m  symmetric min(fair, mkt) - 1c (the live rule before v2)", 0.01, 0.01, ref="min")
        evaluate(t, "A    favourite 2c, underdog 10c", 0.02, 0.10)
        evaluate(t, "Am   A with ref=min", 0.02, 0.10, ref="min")
        evaluate(t, "B    A + from second 100", 0.02, 0.10, rel_lo=100)
        evaluate(t, "D    B + Binance gate (0.5 bp over 3 s)", 0.02, 0.10, rel_lo=100, gate="pre_03")
        evaluate(t, "V2   ref=min, fav 1.5c, dog 10c, second >= 100, gate, p_hi 0.80", 0.015, 0.10, ref="min", rel_lo=100, gate="pre_03", pmax=0.80)
        evaluate(t, "V2b  V2 with fav 1c", 0.01, 0.10, ref="min", rel_lo=100, gate="pre_03", pmax=0.80)
        evaluate(t, "V2c  V2 with fav 2c", 0.02, 0.10, ref="min", rel_lo=100, gate="pre_03", pmax=0.80)
        evaluate(t, "V2d  V2 without the gate", 0.015, 0.10, ref="min", rel_lo=100, pmax=0.80)
        evaluate(t, "V2e  V2 from second 60", 0.015, 0.10, ref="min", rel_lo=60, gate="pre_03", pmax=0.80)
        evaluate(t, "V2f  V2 favourite side only", 0.015, 0.5, ref="min", rel_lo=100, gate="pre_03", pmax=0.80)
        evaluate(t, "V2g  V2 with p_hi 0.90", 0.015, 0.10, ref="min", rel_lo=100, gate="pre_03", pmax=0.90)
