"""Fresh replay (18-21 Nov, 5m, lag 1 s, edge >= 0.15, up to 3 entries per window 15 s apart): by coin, price, time before the
end, edge size, and the seconds since the strike (rel) - looking for a segment that loses."""
import sys, collections
sys.path.insert(0, sys.argv[1])
from fv2 import events, rep
ev = events("5m", 1)
sel, last, cnt = [], {}, collections.Counter()
for e in ev:
    if e["edge"] < 0.15 or not 0.05 <= e["px"] <= 0.95 or e["sd"] != "B" or not 62 <= e["x"] <= 290: continue
    s = e["slug"]
    if cnt[s] >= 3 or (s in last and e["t"] - last[s] < 15): continue
    cnt[s] += 1; last[s] = e["t"]; sel.append(e)
rep(sel, "all")
for a in ("btc", "eth", "sol", "xrp"): rep([e for e in sel if e["a"] == a], f"coin {a}")
for lo, hi in ((0.05, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 0.95)):
    rep([e for e in sel if lo <= e["px"] < hi], f"price {lo:.2f}-{hi:.2f}")
for lo, hi in ((62, 90), (90, 120), (120, 160), (160, 200), (200, 240), (240, 291)):
    rep([e for e in sel if lo <= e["x"] < hi], f"s before end {lo}-{hi}")
for lo, hi in ((0.15, 0.20), (0.20, 0.25), (0.25, 0.35), (0.35, 1.0)):
    rep([e for e in sel if lo <= e["edge"] < hi], f"edge {lo:.2f}-{hi:.2f}")
