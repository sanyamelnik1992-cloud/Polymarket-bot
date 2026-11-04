"""Load recorder output and rebuild per-token order books / top-of-book time series."""
import json
from collections import defaultdict

import numpy as np
import pandas as pd


def read_jsonl(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def load_markets(d):
    rows = {}
    for m in read_jsonl(f"{d}/markets.jsonl"):
        rows[m["slug"]] = m
    return pd.DataFrame(rows.values())


def load_prices(d):
    cl = pd.DataFrame(read_jsonl(f"{d}/chainlink.jsonl"))
    bn = pd.DataFrame(read_jsonl(f"{d}/binance.jsonl"))
    if not bn.empty:
        bn["mid"] = (bn.bid + bn.ask) / 2
    return cl, bn


def rebuild_books(d, depth_levels=3, sample_ms=None):
    """Replay book snapshots + price_change deltas.

    Returns DataFrame of top-of-book updates per token key:
      rx, ts, k, bid, ask, bid_sz, ask_sz, bid_depth3, ask_depth3
    plus a DataFrame of trades (last_trade_price events).
    """
    books = defaultdict(lambda: ({}, {}))  # k -> (bids{price:size}, asks{price:size})
    out = []
    trades = []

    def snap(k, rx, ts):
        b, a = books[k]
        bb = sorted(b.items(), key=lambda x: -x[0])[:depth_levels]
        aa = sorted(a.items(), key=lambda x: x[0])[:depth_levels]
        out.append((rx, ts, k,
                    bb[0][0] if bb else np.nan, aa[0][0] if aa else np.nan,
                    bb[0][1] if bb else 0.0, aa[0][1] if aa else 0.0,
                    sum(s for _, s in bb), sum(s for _, s in aa)))

    for ev in read_jsonl(f"{d}/clob.jsonl"):
        e = ev.get("e")
        if e == "book":
            k = ev["k"]
            books[k] = ({p: s for p, s in ev["b"] if s > 0}, {p: s for p, s in ev["a"] if s > 0})
            snap(k, ev["rx"], ev["ts"])
        elif e == "pc":
            touched = set()
            for k, p, s, side, _bb, _ba in ev["c"]:
                b, a = books[k]
                book = b if side == "B" else a
                if s <= 0:
                    book.pop(p, None)
                else:
                    book[p] = s
                touched.add(k)
            for k in touched:
                snap(k, ev["rx"], ev["ts"])
        elif e == "conn" and ev.get("state") == "down":
            # feed gap: invalidate this asset's books so stale quotes are never used
            for k in [k for k in books if k.startswith(ev["asset"] + "-")]:
                books[k] = ({}, {})
                snap(k, ev["rx"], ev["rx"])
        elif e == "trade":
            trades.append((ev["rx"], ev["ts"], ev["k"], ev["p"], ev["s"], ev.get("side")))
    tob = pd.DataFrame(out, columns=["rx", "ts", "k", "bid", "ask", "bid_sz", "ask_sz", "bid_d3", "ask_d3"])
    tr = pd.DataFrame(trades, columns=["rx", "ts", "k", "price", "size", "side"])
    for df in (tob, tr):
        parts = df["k"].str.split("-", expand=True)
        df["asset"], df["window_start"], df["outcome"] = parts[0], parts[1].astype(int), parts[2]
    return tob, tr
