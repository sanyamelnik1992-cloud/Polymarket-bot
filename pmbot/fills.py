"""Per-fill breakdown of a journal run: edge = (1 if our side won else 0) - price, the model fair value and the book
mid just before the fill (from the 1 Hz "fv" trace), and the 10 s markout (fair 10 s later - fair at the fill;
< 0 = we were picked off). Used by scripts/fill_breakdown.py and pmbot/review.py.
"""
import bisect
import json
import os
from collections import defaultdict

import pandas as pd


def load(run_dir):
    fills, winners, fv = [], {}, defaultdict(list)
    with open(os.path.join(run_dir, "events.jsonl")) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "fv":
                fv[e["key"]].append((e["t"] * 1000, e["p"], e.get("mid")))
            elif t == "fill":
                fills.append(e)
            elif t == "settle":
                winners[e["key"]] = e["winner"]
    rows = []
    for f in fills:
        k = f["key"]
        if k not in winners:
            continue
        h = fv.get(k, [])
        ts = [x[0] for x in h]
        i = bisect.bisect_right(ts, f["ts"]) - 1
        j = bisect.bisect_left(ts, f["ts"] + 10_000)
        tok = (lambda q: q) if f["side"] == "U" else (lambda q: 1 - q)
        fair = tok(h[i][1]) if i >= 0 else None
        mid = tok(h[i][2]) if i >= 0 and h[i][2] is not None else None
        won = (winners[k] == "Up") == (f["side"] == "U")
        rows.append(dict(key=k, asset=k.split("-")[0], side=f["side"], price=f["price"], size=f["size"],
                         rel=f["ts"] / 1000 - int(k.split("-")[1]), won=won,
                         edge=(1.0 if won else 0.0) - f["price"], fair=fair, mid=mid,
                         mk10=(tok(h[j][1]) - fair) if (fair is not None and j < len(h)) else None))
    d = pd.DataFrame(rows)
    if len(d):
        d["pnl"] = d.edge * d["size"]
        d["dis"] = (d.fair - d.mid).abs()
    return d


def table(d, col, bins):
    g = d.groupby(pd.cut(d[col], bins, include_lowest=True), observed=True)
    return g.agg(fills=("pnl", "size"), pnl=("pnl", "sum"), edge=("edge", "mean"), mk10=("mk10", "mean")).round(3)
