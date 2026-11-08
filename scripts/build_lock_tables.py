"""Build config/lock_tables.npz (empirical future-average tails for pmbot/lockmodel.py) from recorder Chainlink files.

  python scripts/build_lock_tables.py OUT.npz data/rec/20261107-0811/chainlink.jsonl data/rec/20261108-0811/chainlink.jsonl
Each recording is a separate 1 s series (no fake returns across the gap between recordings).
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import CHAINLINK_SYMBOL  # noqa: E402
from pmbot.lockmodel import build_tables  # noqa: E402


def series(path, sym):
    d = {}
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("sym") == sym:
                d.setdefault(r["ts"] // 1000, r["v"])
    if not d:
        return None
    lo, hi = min(d), max(d)
    out, last = [], None
    for s in range(lo, hi + 1):
        last = d.get(s, last)
        out.append(last)
    return np.array(out, dtype=float)


def main():
    out, paths = sys.argv[1], sys.argv[2:]
    merged = None
    for p in paths:
        parts = {a: series(p, sym) for a, sym in CHAINLINK_SYMBOL.items()}
        parts = {a: v for a, v in parts.items() if v is not None and len(v) > 3600}
        t = build_tables(parts)
        n = {a: len(v) for a, v in parts.items()}
        if merged is None:
            merged, weights = t, n
        else:            # sample-weighted average of the survival functions of the recordings
            for a in parts:
                k = f"s_{a}"
                if k in merged:
                    w0, w1 = weights[a], n[a]
                    merged[k] = ((merged[k] * w0 + t[k] * w1) / (w0 + w1)).astype(np.float32)
                    weights[a] = w0 + w1
        print(p, n)
    np.savez_compressed(out, **merged)
    print("wrote", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
