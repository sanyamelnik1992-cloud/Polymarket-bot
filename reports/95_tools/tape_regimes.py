"""Calibration of the model and regime tables of one quoting rule on the tape (table.npz from tape_table.py).

  python tape_regimes.py TABLE.npz [H_FAV=0.015] [H_DOG=0.10]

1. calibration of the model's fair value (all trades, the maker's side) and of the trade price, by bucket: the model is too close to
   0.5 (underdogs win 2-3 c less than fair, favourites 1-3 c more), the trade price is calibrated within +-1 c;
2. the regression outcome ~ fair + market: how much weight each deserves (in a flat Binance market the market is worth as much as the
   model; after a Binance move the model is worth three times as much);
3. the rule (ref = min(fair, market), from second 100, Binance gate) by side, fair value, second, volatility, Binance move, depth."""
import sys
import numpy as np
from scipy.special import ndtr, ndtri

t = {k: v for k, v in np.load(sys.argv[1]).items()}
HF = float(sys.argv[2]) if len(sys.argv) > 2 else 0.015
HD = float(sys.argv[3]) if len(sys.argv) > 3 else 0.10
days = np.unique(t["day"])
won = t["pnl"] + t["pl"]
w = np.minimum(t["n"], 5.0)
base = (t["rel"] >= 20) & (t["rel"] <= 230) & np.isfinite(t["fL"])
print("calibration of the model (all trades): bucket  n  fair  won  diff(c)  by day")
for lo, hi in ((0.05, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95)):
    m = base & (t["fL"] >= lo) & (t["fL"] < hi)
    ww = w[m]
    ds = " ".join(f"{100 * ((won[m & (t['day'] == d)] - t['fL'][m & (t['day'] == d)]) * w[m & (t['day'] == d)]).sum() / w[m & (t['day'] == d)].sum():+5.1f}" for d in days)
    print(f"  {lo:.2f}-{hi:.2f} {m.sum():8d} {(t['fL'][m] * ww).sum() / ww.sum():.3f} {(won[m] * ww).sum() / ww.sum():.3f} "
          f"{100 * ((won[m] - t['fL'][m]) * ww).sum() / ww.sum():+6.2f}   {ds}")
print("calibration of the trade price (maker's side price): bucket  n  price  won  diff(c)")
for lo, hi in ((0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0001)):
    m = base & (t["pl"] >= lo) & (t["pl"] < hi)
    ww = w[m]
    print(f"  {lo:.1f}-{hi:.1f} {m.sum():8d} {(t['pl'][m] * ww).sum() / ww.sum():.3f} {(won[m] * ww).sum() / ww.sum():.3f} {100 * ((won[m] - t['pl'][m]) * ww).sum() / ww.sum():+6.2f}")
ok = base & np.isfinite(t["mL"])
X = np.column_stack([np.ones(ok.sum()), t["fL"][ok], t["mL"][ok]])
b = np.linalg.lstsq(X * np.sqrt(w[ok])[:, None], won[ok] * np.sqrt(w[ok]), rcond=None)[0]
print(f"won ~ a + b fair + c market (all trades): a {b[0]:+.3f} b {b[1]:.2f} c {b[2]:.2f}")
for lo, hi in ((-99, -1.5), (-1.5, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 99)):
    m = ok & (t["pre_03"] >= lo) & (t["pre_03"] < hi)
    X = np.column_stack([np.ones(m.sum()), t["fL"][m], t["mL"][m]])
    b = np.linalg.lstsq(X * np.sqrt(w[m])[:, None], won[m] * np.sqrt(w[m]), rcond=None)[0]
    print(f"   Binance move toward the side {lo:+.1f}..{hi:+.1f} bp: weight on fair {b[1]:.2f}, on market {b[2]:.2f}")

f = t["fL"].copy()
r_ = np.where(np.isfinite(t["mL"]), np.minimum(f, t["mL"]), f)
fav = t["fL"] >= 0.5
bid = np.floor((r_ - np.where(fav, HF, HD)) * 100 + 1e-9) / 100
m0 = base & (r_ >= 0.10) & (r_ <= 0.90) & (bid >= 0.05) & (t["pl"] <= bid + 1e-9) & (t["rel"] >= 100) & ~(t["pre_03"] < -0.5)
pn = (won - bid) * w
print(f"\nrule (ref=min, fav {HF}, dog {HD}, second >= 100, gate 0.5 bp / 3 s): {100 * pn[m0].sum() / w[m0].sum():+.2f} c/sh, {w[m0].sum() / len(days):.0f} sh/day")


def row(m, label, minsh=1500):
    m = m & m0
    ww = w[m]
    if ww.sum() < minsh:
        return
    ds = " ".join(f"{100 * pn[m & (t['day'] == d)].sum() / max(w[m & (t['day'] == d)].sum(), 1):+5.1f}" for d in days)
    print(f"  {label:26s} sh/day {ww.sum() / len(days):7.0f}  pnl {100 * pn[m].sum() / ww.sum():+6.2f} c   by day {ds}")


print("by side:"); row(fav, "favourite"); row(~fav, "underdog")
print("by second:")
for lo, hi in ((100, 140), (140, 180), (180, 231)):
    row((t["rel"] >= lo) & (t["rel"] < hi), f"second {lo}-{hi}")
print("by sigma (bp per sqrt s):")
for lo, hi in ((0, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 1.4), (1.4, 9)):
    row((t["sg"] >= lo) & (t["sg"] < hi), f"sigma {lo}-{hi}")
print("by Binance move toward the side over 3 s (bp):")
for lo, hi in ((-0.5, 0.0), (0, 0.5), (0.5, 1.5), (1.5, 99)):
    row((t["pre_03"] >= lo) & (t["pre_03"] < hi), f"{lo:+.1f}..{hi:+.1f}")
