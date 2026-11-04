"""Collect closed hourly crypto Up/Down markets (resolve on the Binance 1H candle), trades and Binance 1s klines.

usage: python scripts/collect_hourly.py OUT_DIR DAYS [assets=btc,eth]
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import BINANCE_SYMBOL, GAMMA  # noqa: E402
from pmbot.http import get_json  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from collect_history import collect_all_trades, collect_klines  # noqa: E402

SERIES = {"btc": "btc-up-or-down-hourly", "eth": "eth-up-or-down-hourly", "sol": "solana-up-or-down-hourly",
          "xrp": "xrp-up-or-down-hourly"}


def events(series, days):
    out, offset = [], 0
    since = datetime.now(timezone.utc) - timedelta(days=days)
    while True:
        d = get_json(f"{GAMMA}/events", {"series_slug": series, "closed": "true", "limit": 100, "offset": offset,
                                         "order": "endDate", "ascending": "false"}, timeout=90)
        if not d:
            return out
        for e in d:
            if datetime.fromisoformat(e["endDate"].replace("Z", "+00:00")) >= since:
                out.append(e)
        if datetime.fromisoformat(d[-1]["endDate"].replace("Z", "+00:00")) < since:
            return out
        offset += 100


def parse(e, asset):
    m = e["markets"][0]
    toks = json.loads(m["clobTokenIds"])
    outs = json.loads(m["outcomes"])
    prices = json.loads(m["outcomePrices"])
    up = outs.index("Up")
    start = int(datetime.fromisoformat(m["eventStartTime"].replace("Z", "+00:00")).timestamp())
    meta = e.get("eventMetadata") or {}
    return {"asset": asset, "slug": e["slug"], "condition_id": m["conditionId"], "token_up": toks[up],
            "token_down": toks[1 - up], "window_start": start, "window": 3600,
            "winner": "Up" if float(prices[up]) == 1.0 else "Down", "price_to_beat": meta.get("priceToBeat"),
            "final_price": meta.get("finalPrice"), "volume": float(m.get("volume") or 0)}


if __name__ == "__main__":
    out, days = sys.argv[1], float(sys.argv[2])
    assets = sys.argv[3].split(",") if len(sys.argv) > 3 else ["btc", "eth"]
    os.makedirs(out, exist_ok=True)
    rows = []
    for a in assets:
        rows += [parse(e, a) for e in events(SERIES[a], days)]
    mk = pd.DataFrame(rows).drop_duplicates("condition_id")
    mk.to_parquet(f"{out}/markets.parquet")
    print(f"markets {len(mk)}", mk.groupby("asset").size().to_dict(), flush=True)
    start, end = int(mk.window_start.min()) - 1900, int(mk.window_start.max()) + 3700
    with ThreadPoolExecutor(len(assets)) as ex:
        kls = list(ex.map(lambda a: collect_klines(BINANCE_SYMBOL[a], start, end), assets))
    for a, k in zip(assets, kls):
        k.to_parquet(f"{out}/klines_{a}.parquet")
    print("klines done", flush=True)
    collect_all_trades(list(mk.condition_id)).to_parquet(f"{out}/trades.parquet")
    print("trades done", flush=True)
