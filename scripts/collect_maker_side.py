"""Fetch both maker and taker rows (taker_only=false) for a set of markets.

usage: python scripts/collect_maker_side.py HIST_DIR ASSET HOURS
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import DATA  # noqa: E402
from pmbot.http import get_json  # noqa: E402


def rows_for(cond):
    out, cur = [], None
    while True:
        p = {"condition": cond, "limit": 1000, "taker_only": "false"}
        if cur:
            p["cursor"] = cur
        d = get_json(f"{DATA}/trades", p)
        out += d["data"]
        pg = d.get("pagination") or {}
        if not pg.get("has_more") or not pg.get("next_cursor"):
            return out
        cur = pg["next_cursor"]


if __name__ == "__main__":
    hist, asset, hours = sys.argv[1], sys.argv[2], float(sys.argv[3])
    mk = pd.read_parquet(f"{hist}/markets.parquet")
    mk = mk[(mk.asset == asset) & (mk.window_start >= mk.window_start.max() - hours * 3600)]
    with ThreadPoolExecutor(12) as ex:
        allrows = [r for rows in ex.map(rows_for, mk.condition_id) for r in rows]
    df = pd.DataFrame(allrows)
    print(len(mk), "markets", len(df), "rows", df.columns.tolist())
    df.to_parquet(f"{hist}/trades_all_{asset}.parquet")
