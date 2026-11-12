"""How a farmer exits after a maker fill: next taker trade of the same wallet in the same market.
usage: hedge.py PROF.json"""
import json, sys, collections
import numpy as np, pandas as pd
d = json.load(open(sys.argv[1]))
mk = pd.DataFrame([dict(cid=r["condition_id"], tok=r["token_id"], side=r["side"], size=float(r["size"]), price=float(r["price"]), ts=r["timestamp"], oi=r.get("outcome_index"), tx=r["transaction_hash"]) for r in d["maker"]])
tk = pd.DataFrame([dict(cid=r["condition_id"], tok=r["token_id"], side=r["side"], size=float(r["size"]), price=float(r["price"]), ts=r["timestamp"], oi=r.get("outcome_index"), tx=r["transaction_hash"]) for r in d["taker"]])
print("maker fills", len(mk), " taker trades", len(tk), " maker BUY share %.2f, taker BUY share %.2f" % ((mk.side == "BUY").mean(), (tk.side == "BUY").mean()))
tk_by = {c: g.sort_values("ts") for c, g in tk.groupby("cid")}
res = []
for _, f in mk.iterrows():
    g = tk_by.get(f.cid)
    if g is None: res.append((f, None)); continue
    after = g[(g.ts >= f.ts) & (g.ts <= f.ts + 3600)]
    # an exit of a BUY of token X: taker SELL of X, or taker BUY of the other token
    if f.side == "BUY":
        ex = after[((after.tok == f.tok) & (after.side == "SELL")) | ((after.tok != f.tok) & (after.side == "BUY"))]
    else:
        ex = after[((after.tok == f.tok) & (after.side == "BUY")) | ((after.tok != f.tok) & (after.side == "SELL"))]
    res.append((f, ex.iloc[0] if len(ex) else None))
rows = []
for f, e in res:
    if e is None: rows.append(dict(hedged=0, usd=f["size"] * f.price)); continue
    if f.side == "BUY":
        loss = (f.price + e.price - 1) if e.tok != f.tok else (f.price - e.price)
    else:
        loss = (e.price - f.price) if e.tok == f.tok else (1 - e.price - f.price)
    rows.append(dict(hedged=1, delay=e.ts - f.ts, loss=loss, fsize=f["size"], esize=e["size"], price=f.price, via="complement" if e.tok != f.tok else "same", usd=f["size"] * f.price))
r = pd.DataFrame(rows)
h = r[r.hedged == 1]
print("fills followed by an exit trade within 1 h: %.0f%% (%d of %d)" % (100 * r.hedged.mean(), len(h), len(r)))
if len(h):
    q = lambda s, ps=(.1, .25, .5, .75, .9): np.round(np.quantile(s, ps), 3)
    print(" exit via:", dict(collections.Counter(h.via)))
    print(" delay s quantiles 10/25/50/75/90:", q(h.delay))
    print(" loss per share (price units) quantiles:", q(h.loss), " mean %.4f; size-weighted mean %.4f" % (h.loss.mean(), (h.loss * h.fsize).sum() / h.fsize.sum()))
    print(" loss as %% of fill notional: mean %.1f%%" % (100 * (h.loss * h.fsize).sum() / (h.price * h.fsize).sum()))
    fast = h[h.delay <= 5]
    print(" exits within 5 s: %d (%.0f%%), loss per share mean %.4f median %.4f" % (len(fast), 100 * len(fast) / len(h), fast.loss.mean(), fast.loss.median()))
    for lo, hi in ((0, 2), (2, 10), (10, 60), (60, 600), (600, 3601)):
        k = h[(h.delay >= lo) & (h.delay < hi)]
        if len(k): print(f"   delay {lo:4d}-{hi:4d}s: n={len(k):4d} loss/share mean {k.loss.mean():+.4f} median {k.loss.median():+.4f}")
