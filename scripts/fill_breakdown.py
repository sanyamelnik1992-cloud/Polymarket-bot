"""Per-fill breakdown of paper/live journals: where the money is made and lost.

For every settled fill: edge = (1 if our side won else 0) - price, the model fair value and the book mid just before
the fill (from the 1 Hz "fv" trace), and the 10 s markout (fair 10 s later - fair at the fill; < 0 = picked off).

  python scripts/fill_breakdown.py data/journal-colo-v2/*/ data/journal-colo-v2-strict/*/
"""
import argparse
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="journal run directories")
    a = ap.parse_args()
    pd.set_option("display.width", 200)
    for r in a.runs:
        d = load(r)
        print(f"=== {r}: {len(d)} settled fills")
        if not len(d):
            continue
        w = d.groupby("key").pnl.sum()
        t = w.mean() / w.std() * len(w) ** 0.5 if len(w) > 1 and w.std() > 0 else float("nan")
        print(f"fill pnl ${d.pnl.sum():+.2f} (ex rebate) over {len(w)} windows, t {t:+.2f}, "
              f"mean 10s markout {d.mk10.mean():+.3f}\n")
        print("by |fair - mid| at the fill (max_disagree):\n", table(d, "dis", [0, .02, .04, .06, .08, 1]), "\n")
        print("by seconds into the window:\n", table(d, "rel", [0, 30, 60, 120, 180, 240, 300]), "\n")
        print("by fill price:\n", table(d, "price", [0, .2, .35, .5, .65, .8, 1]), "\n")
        print(d.groupby("asset").agg(fills=("pnl", "size"), pnl=("pnl", "sum"), mk10=("mk10", "mean")).round(3), "\n")


if __name__ == "__main__":
    main()
