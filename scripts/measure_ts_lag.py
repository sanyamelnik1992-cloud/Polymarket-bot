"""Measure Data API trade timestamp lag vs the live CLOB match time (needs a recorder dir).

Unique (token, price, size) fills seen both on the market WebSocket (ms match time) and in Data API v2
are matched; lag = data-api timestamp - ws match time.

usage: python scripts/measure_ts_lag.py LIVE_DIR [n_markets=8]
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import DATA  # noqa: E402
from pmbot.http import get_json  # noqa: E402
from pmbot.live_data import load_markets, read_jsonl  # noqa: E402

if __name__ == "__main__":
    d = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    mk = load_markets(d)
    ws = pd.DataFrame([(e["ts"], e["k"], e["p"], e["s"], e.get("side")) for e in read_jsonl(f"{d}/clob.jsonl")
                       if e.get("e") == "trade"], columns=["ts", "k", "p", "s", "side"])
    out = []
    for _, m in mk[mk.asset.isin(["btc", "eth"])].sort_values("window_start").head(n).iterrows():
        rows, cur = [], None
        while True:
            p = {"condition": m.condition_id, "limit": 1000}
            if cur:
                p["cursor"] = cur
            r = get_json(f"{DATA}/trades", p)
            rows += r["data"]
            if not r["pagination"].get("has_more"):
                break
            cur = r["pagination"]["next_cursor"]
        if not rows:
            continue
        da = pd.DataFrame(rows)
        da["k"] = np.where(da.token_id == m.token_up, f"{m.asset}-{m.window_start}-U", f"{m.asset}-{m.window_start}-D")
        w = ws[ws.k.str.startswith(f"{m.asset}-{m.window_start}-")].copy()
        w["key"] = w.k + "|" + w.p.round(4).astype(str) + "|" + w.s.round(4).astype(str)
        da["key"] = da.k + "|" + da.price.round(4).astype(str) + "|" + da["size"].round(4).astype(str)
        w, da = w[~w.key.duplicated(keep=False)], da[~da.key.duplicated(keep=False)]
        j = w.merge(da[["key", "timestamp", "side"]], on="key", suffixes=("", "_api"))
        j["lag"] = j.timestamp - j.ts / 1000
        out.append(j)
    j = pd.concat(out)
    print(f"matched unique fills: {len(j)}; taker side agrees: {(j.side == j.side_api).mean():.3f}")
    print(j.lag.describe(percentiles=[.05, .25, .5, .75, .95]).round(2))
