"""Losers at high z: a real reversal (the Binance close proxy also flipped) or a proxy disagreement with Gamma?"""
import json, time, numpy as np
from zmod import *
W = json.load(open("windows.json"))
for kind, x, z0 in (("15m", 120, 4), ("15m", 180, 4), ("15m", 240, 3.5), ("5m", 120, 3.5), ("5m", 90, 4), ("1h", 180, 4), ("1h", 300, 3)):
    print(f"\n{kind} x={x} z>={z0}")
    for w in W:
        if w["kind"] != kind: continue
        t = w["end"] - x
        lead, sg, z = z_at(w, t)
        if abs(z) < z0: continue
        side = "Up" if lead > 0 else "Down"
        if side == w["win"]: continue
        K, f = strike(w), final(w)
        fl = np.log(f / K) * 1e4
        kind_l = "REVERSAL" if (fl > 0) != (lead > 0) else "proxy says leader won"
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(w['start']))} {w['slug']:28s} lead {lead:+6.1f}bp sigma {sg:.2f} z {z:+5.1f} final {fl:+6.1f}bp -> {w['win']:4s} {kind_l}")
