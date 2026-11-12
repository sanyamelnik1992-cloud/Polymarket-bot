"""What is market_competitiveness? Score every book with the documented formula and compare.
Result (11 Nov, 1771 markets): competitiveness x rate_per_day = the total qualifying score (median ratio 1.09, log
correlation 0.91), i.e. the number is "points of all makers per $1 of the daily pool".
usage: competitiveness.py ACTIVE.json BOOKS.json   (scale.py writes active.json, books.py the books)"""
import json, sys
import numpy as np
act = {m["tokens"][0]["token_id"]: m for m in json.load(open(sys.argv[1])) if len(m.get("tokens") or []) == 2}
books = json.load(open(sys.argv[2]))["books"]
def levels(raw):
    b = sorted(((float(x["price"]), float(x["size"])) for x in raw.get("bids") or [] if float(x["size"]) > 0), reverse=True)
    a = sorted((float(x["price"]), float(x["size"])) for x in raw.get("asks") or [] if float(x["size"]) > 0)
    return b, a
def adj(lv, ms):
    cum = 0.0
    for p, s in lv:
        cum += s
        if cum >= ms - 1e-9: return p
    return None
comp, rate, q1, q2, mid = [], [], [], [], []
for t, raw in books.items():
    m = act.get(t)
    if not m: continue
    v = float(m.get("rewards_max_spread") or 0); ms = float(m.get("rewards_min_size") or 0)
    if v <= 0: continue
    b, a = levels(raw)
    pb, pa = adj(b, ms), adj(a, ms)
    md = (pb + pa) / 2 if pb is not None and pa is not None else ((b[0][0] + a[0][0]) / 2 if b and a else None)
    if md is None: continue
    q = lambda lv: sum(((v - abs(p - md) * 100) / v) ** 2 * s for p, s in lv if abs(p - md) * 100 < v)
    comp.append(float(m.get("market_competitiveness") or 0)); rate.append(m["_rate"]); q1.append(q(b)); q2.append(q(a)); mid.append(md)
comp, rate, q1, q2, mid = map(np.array, (comp, rate, q1, q2, mid))
inr = (mid >= 0.10) & (mid <= 0.90)
qmin = np.where(inr, np.maximum(np.minimum(q1, q2), np.maximum(q1, q2) / 3), np.minimum(q1, q2))
print("markets", len(comp), " competitiveness 0:", int((comp == 0).sum()), " no score in the book:", int((qmin == 0).sum()), " both:", int(((comp == 0) & (qmin == 0)).sum()))
for name, x in (("Q_min of the whole book", qmin), ("sum of both sides", q1 + q2)):
    for label, y in (("competitiveness x rate", comp * rate), ("competitiveness", comp)):
        k = (y > 0) & (x > 0)
        print(f"{name:24s} vs {label:22s} n={k.sum():5d} corr(log)={np.corrcoef(np.log(y[k]), np.log(x[k]))[0, 1]:.3f}  ratio quantiles 10/25/50/75/90: {np.round(np.quantile(x[k] / y[k], [.1, .25, .5, .75, .9]), 2)}")
