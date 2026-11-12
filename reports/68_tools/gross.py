"""Gross reward a min-size two-sided quote would earn, from the official competitiveness (total score per $ of daily pool)."""
import json, re, time
import numpy as np
act = json.load(open("active.json"))
now = time.time()
from datetime import datetime
def end_ts(m):
    s = m.get("end_date")
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T").replace("+00", "+00:00") if str(s).endswith("+00") else str(s).replace("Z", "+00:00").replace(" ", "T")).timestamp()
    except Exception: return float("nan")
rows = []
for m in act:
    v = float(m.get("rewards_max_spread") or 0); ms = float(m.get("rewards_min_size") or 0)
    if v <= 0 or ms <= 0 or len(m.get("tokens") or []) != 2: continue
    rate = m["_rate"]; comp = float(m.get("market_competitiveness") or 0)
    p = float(m["tokens"][0].get("price") or 0)
    rows.append(dict(rate=rate, comp=comp, v=v, ms=ms, p=p, spread=float(m.get("spread") or 0), vol=float(m.get("volume_24hr") or 0),
                     chg=abs(float(m.get("one_day_price_change") or 0)), days=(end_ts(m) - now) / 86400, q=m["question"], slug=m.get("market_slug"),
                     cid=m["condition_id"], created=m.get("created_at")))
import pandas as pd
d = pd.DataFrame(rows)
print("markets", len(d), "pool", round(d.rate.sum()))
def rew(d, dist, mult=1.0):
    S = d.ms * mult
    f = np.clip((d.v - dist) / d.v, 0, None) ** 2
    Q = S * f
    tot = d.comp * d.rate
    share = np.where(Q > 0, Q / (tot + Q), 0.0)
    two_sided_needed = (d.p < 0.10) | (d.p > 0.90)
    return d.rate * share, S * 1.0          # reward/day, capital ~ $1 per share of two-sided size
for dist in (1.0, 2.0, 3.0):
    r, cap = rew(d, dist)
    d[f"r{dist:.0f}"] = r; d["cap"] = cap
print("\nBy competitiveness bucket (min-size two-sided quote, reward in $/day):")
print(f"{'comp':>12s} {'mkts':>6s} {'pool$':>8s} {'cap$':>8s} {'rew@1c':>8s} {'rew@2c':>8s} {'rew@3c':>8s} {'yield@2c/day':>12s} {'med vol24':>9s} {'med spread':>10s} {'med |chg1d|':>11s}")
for lo, hi in ((0, 1e-9), (1e-9, 1), (1, 5), (5, 20), (20, 50), (50, 150), (150, 500), (500, 1e12)):
    k = d[(d.comp >= lo) & (d.comp < hi)]
    if not len(k): continue
    print(f"[{lo:g},{hi:g})".rjust(12), f"{len(k):6d} {k.rate.sum():8.0f} {k.cap.sum():8.0f} {k.r1.sum():8.0f} {k.r2.sum():8.0f} {k.r3.sum():8.0f} {100*k.r2.sum()/k.cap.sum():11.1f}% {k.vol.median():9.0f} {k.spread.median():10.3f} {k.chg.median():11.3f}")
print("\nprice outside [0.10, 0.90]:", int(((d.p < .1) | (d.p > .9)).sum()), " days to end quantiles 10/50/90:", np.round(d.days.quantile([.1, .5, .9]).values, 1), " ending < 2 days:", int((d.days < 2).sum()))
d.to_pickle("gross.pkl")
# total if we quote EVERY market at min size, 2c
print("\nEvery market, min size, 2c from mid: capital $%.0f, reward $%.0f/day" % (d.cap.sum(), d.r2.sum()))
k = d[(d.p >= .1) & (d.p <= .9) & (d.days > 2)]
print("price 0.10-0.90 and > 2 days left: markets %d, capital $%.0f, reward@2c $%.0f/day" % (len(k), k.cap.sum(), k.r2.sum()))
