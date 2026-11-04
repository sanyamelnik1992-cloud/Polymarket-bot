"""Paired comparison of two bot runs over the same 5m windows (e.g. v1 vs v2 filters).

usage: python scripts/compare_runs.py RUN_DIR_A RUN_DIR_B
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

pd.set_option("display.width", 200)


def load(d):
    ev = [json.loads(line) for line in open(os.path.join(d, "events.jsonl")) if line.strip()]
    st = pd.DataFrame([e for e in ev if e["type"] == "settle"]).set_index("key")
    return ev, st


def blocked_time(ev):
    """Seconds each filter reason was active, summed over windows (from state-change events)."""
    out = defaultdict(float)
    last = {}
    for e in ev:
        if e["type"] != "filters":
            continue
        k = e["key"]
        if k in last:
            t0, reasons = last[k]
            for r in reasons:
                out[r] += e["t"] - t0
        last[k] = (e["t"], e["reasons"])
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def main(a, b):
    ea, sa = load(a)
    eb, sb = load(b)
    common = sorted(set(sa.index) | set(sb.index))
    df = pd.DataFrame({"A": sa.pnl.reindex(common).fillna(0.0), "B": sb.pnl.reindex(common).fillna(0.0)})
    # only windows both runs were alive for
    t_a = (ea[0]["t"], ea[-1]["t"])
    t_b = (eb[0]["t"], eb[-1]["t"])
    lo, hi = max(t_a[0], t_b[0]), min(t_a[1], t_b[1])
    starts = pd.Series([int(k.split("-")[1]) for k in df.index], index=df.index)
    df = df[(starts >= lo) & (starts + 300 <= hi)]
    df["asset"] = [k.split("-")[0] for k in df.index]
    df["diff"] = df.B - df.A
    print(f"overlapping windows: {len(df)}  (A traded {(df.A != 0).sum()}, B traded {(df.B != 0).sum()})")
    print(df.groupby("asset")[["A", "B", "diff"]].sum().round(2))
    tot = df[["A", "B"]].sum()
    t = df["diff"].mean() / df["diff"].std() * np.sqrt(len(df)) if df["diff"].std() > 0 else float("nan")
    print(f"\nTOTAL  A ${tot.A:+.2f}   B ${tot.B:+.2f}   paired diff B-A ${df['diff'].sum():+.2f} (t {t:+.2f})")
    for name, s in (("A", df.A), ("B", df.B)):
        eq = s.cumsum()
        print(f"{name}: worst window ${s.min():+.2f}, max drawdown ${(eq - eq.cummax()).min():+.2f}, "
              f"losing windows {(s < 0).sum()}/{(s != 0).sum()}")
    print("\nfilter time (s) in B:", {k: round(v) for k, v in list(blocked_time(eb).items())[:12]})
    sides = [e for e in eb if e["type"] == "side_block" and e.get("sides")]
    print(f"toxic side blocks in B: {len(sides)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
