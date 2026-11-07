"""Per-fill breakdown of paper/live journals: where the money is made and lost.

For every settled fill: edge = (1 if our side won else 0) - price, the model fair value and the book mid just before
the fill (from the 1 Hz "fv" trace), and the 10 s markout (fair 10 s later - fair at the fill; < 0 = picked off).

  python scripts/fill_breakdown.py data/journal-colo-v2/*/ data/journal-colo-v2-strict/*/
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fills import load, table  # noqa: E402


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
