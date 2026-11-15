"""Is the model right near the strike? Signal rows of paths.py by expected distance to the strike (dist_bp), one row per
window and bucket (the first), against the exchange's outcome. usage: python reports/71_tools/dist_calib.py DATA_DIR OUT_DIR"""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import variants as v
g, win = v.load(sys.argv[1], sys.argv[2])
s = g[g.sig].copy()
s["rel"] = s.t / 1000 - s["T"]
s["won"] = [(sd == "U") == (win.get((a, int(T))) == "Up") if win.get((a, int(T))) else None for a, T, sd in zip(s.asset, s["T"], s.side)]
s = s[s.won.notna()]; s["won"] = s.won.astype(bool)
B = [(0.5, 0.75), (0.75, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 99)]
def tab(d, name):
    print("==", name)
    for lo, hi in B:
        x = d[(d.dist_bp >= lo) & (d.dist_bp < hi)].groupby(["asset", "T"], sort=False).first()
        if len(x):
            fill = x[np.isfinite(x.x1) & (x.x1 <= x.limit + 1e-9)]
            print(f"  dist {lo:4.2f}-{hi:5.2f} bp: windows {len(x):3d}  model p {x.p.mean():.3f}  won {x.won.mean():.3f} (lost {int((~x.won).sum()):2d})  "
                  f"ask {x.ask.mean():.3f}  rel {x.rel.mean():.0f} | fillable {len(fill):3d} lost {int((~fill.won).sum()):2d} per-share {100*((fill.won*1.0)-fill.x1).mean() if len(fill) else 0:+.1f}c")
tab(s, "all signal rows (live rule, tiers as live before reports/71)")
tab(s[s.rel >= 260], "second >= 260 only")
tab(s[(s.rel >= 260) | (s.p >= 0.97)], "the rule as live now (weak tier from 260)")
