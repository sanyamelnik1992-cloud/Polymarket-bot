"""Collect historical 5m Up/Down markets, their taker trades, and Binance 1s klines.

usage: python scripts/collect_history.py OUT_DIR HOURS [assets=btc,eth,sol,xrp] [duration=5m|15m]

Writes OUT_DIR/markets.parquet, OUT_DIR/trades.parquet, OUT_DIR/klines_<asset>.parquet
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import BINANCE_REST, BINANCE_SYMBOL, DATA, WINDOW  # noqa: E402
from pmbot.gamma import fetch_event, parse_event, slug_for, window_start  # noqa: E402
from pmbot.http import get_json  # noqa: E402


def collect_markets(assets, start, end, window=WINDOW, dur="5m"):
    jobs = [(a, s) for a in assets for s in range(start, end, window)]

    def one(job):
        a, s = job
        e = fetch_event(slug_for(a, s, dur))
        if not e:
            return None
        m = parse_event(e)
        m["asset"], m["window_start"] = a, s
        return m

    with ThreadPoolExecutor(16) as ex:
        rows = [r for r in ex.map(one, jobs) if r]
    return pd.DataFrame(rows)


def collect_trades(cond):
    rows, cursor = [], None
    while True:
        params = {"condition": cond, "limit": 1000}
        if cursor:  # filters must be repeated on every page, otherwise the cursor walks the global feed
            params["cursor"] = cursor
        d = get_json(f"{DATA}/trades", params)
        rows += d["data"]
        pg = d.get("pagination") or {}
        if not pg.get("has_more") or not pg.get("next_cursor"):
            break
        cursor = pg["next_cursor"]
    return rows


def collect_all_trades(conds):
    out = []
    t0 = time.time()
    with ThreadPoolExecutor(12) as ex:
        for i, rows in enumerate(ex.map(collect_trades, conds)):
            out += rows
            if i % 200 == 0:
                print(f"  trades {i}/{len(conds)} rows={len(out)} {time.time()-t0:.0f}s", flush=True)
    df = pd.DataFrame(out)
    keep = ["proxy_wallet", "side", "token_id", "condition_id", "size", "price", "timestamp", "outcome",
            "transaction_hash", "name"]
    return df[[c for c in keep if c in df.columns]]


def collect_klines(symbol, start, end):
    rows = []
    t = start * 1000
    while t < end * 1000:
        k = get_json(f"{BINANCE_REST}/klines", {"symbol": symbol, "interval": "1s", "startTime": t, "limit": 1000})
        if not k:
            break
        rows += k
        t = k[-1][0] + 1000
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tqv", "_"])
    df = df[["t", "o", "h", "l", "c", "v", "n"]].astype({"o": float, "h": float, "l": float, "c": float, "v": float})
    df["t"] = df["t"] // 1000
    return df.drop_duplicates("t").set_index("t")


if __name__ == "__main__":
    out = sys.argv[1]
    hours = float(sys.argv[2])
    assets = sys.argv[3].split(",") if len(sys.argv) > 3 else ["btc", "eth", "sol", "xrp"]
    dur = sys.argv[4] if len(sys.argv) > 4 else "5m"
    window = {"5m": 300, "15m": 900}[dur]
    os.makedirs(out, exist_ok=True)
    end = window_start(time.time(), window) - 2 * window   # last fully resolved windows
    start = window_start(end - hours * 3600, window)

    print("markets...", flush=True)
    mk = collect_markets(assets, start, end, window, dur)
    mk["window"] = window
    mk.to_parquet(f"{out}/markets.parquet")
    print(f"  {len(mk)} markets, resolved={mk['winner'].notna().sum()}", flush=True)

    for a in assets:
        print("klines", a, flush=True)
        collect_klines(BINANCE_SYMBOL[a], start - 120, end + window + 60).to_parquet(f"{out}/klines_{a}.parquet")

    print("trades...", flush=True)
    tr = collect_all_trades(list(mk["condition_id"]))
    tr.to_parquet(f"{out}/trades.parquet")
    print(f"  {len(tr)} trades", flush=True)
