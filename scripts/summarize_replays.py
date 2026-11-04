"""Summarise replay journals: python scripts/summarize_replays.py DIR [DIR...] (each DIR holds variant subdirs)."""
import json
import os
import sys

import numpy as np
import pandas as pd

rows = []
for root in sys.argv[1:]:
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name, "events.jsonl")
        if not os.path.exists(p):
            continue
        st, fills = [], 0
        for line in open(p):
            e = json.loads(line)
            if e["type"] == "settle":
                st.append((e["key"], e["pnl"], e["cost"]))
            elif e["type"] == "fill":
                fills += 1
        df = pd.DataFrame(st, columns=["key", "pnl", "cost"])
        if df.empty:
            rows.append({"set": os.path.basename(root), "variant": name, "windows": 0})
            continue
        t = df.pnl.mean() / df.pnl.std() * np.sqrt(len(df)) if len(df) > 2 and df.pnl.std() > 0 else np.nan
        eq = df.pnl.cumsum()
        rows.append({"set": os.path.basename(root), "variant": name, "windows": len(df), "fills": fills,
                     "pnl": df.pnl.sum(), "cost": df.cost.sum(), "roi": df.pnl.sum() / df.cost.sum(), "t": t,
                     "worst": df.pnl.min(), "max_dd": (eq - eq.cummax()).min(),
                     **{f"pnl_{a}": df[df.key.str.startswith(a)].pnl.sum() for a in ("btc", "eth", "sol")}})
pd.set_option("display.width", 220)
print(pd.DataFrame(rows).round(3).to_string(index=False))
