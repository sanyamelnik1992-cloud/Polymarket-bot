"""Scan all active multi-market events for partition / date-ladder / strike-ladder arbitrage,
then verify candidates against the live CLOB books (executable size and $ profit).

usage: python scripts/ladder_scan.py [min_edge=0.005] [max_pages=5]
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import CLOB, GAMMA  # noqa: E402
from pmbot.fees import fee_per_share  # noqa: E402
from pmbot.http import get_json  # noqa: E402
from pmbot.ladders import ladder_checks, legs_of, partition_check  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 70)


def active_events(max_pages=5):
    out = []
    for page in range(max_pages):
        try:
            d = get_json(f"{GAMMA}/events", {"closed": "false", "active": "true", "limit": 500, "offset": page * 500,
                                             "order": "volume24hr", "ascending": "false"}, timeout=90)
        except Exception:  # noqa: BLE001
            break
        if not d:
            break
        out += d
    return out


def book(token):
    try:
        b = get_json(f"{CLOB}/book", {"token_id": token})
    except Exception:  # noqa: BLE001
        return None
    return {"bids": sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True),
            "asks": sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))}


def depth_partition_yes(legs, books, fee_rates, max_sets=10000):
    """Walk all YES ask ladders together: buy one of each while total unit cost < 1."""
    ladders = [list(books[lg.token_yes]["asks"]) for lg in legs]
    idx = [0] * len(legs)
    left = [lad[0][1] if lad else 0 for lad in ladders]
    sets = profit = 0.0
    while sets < max_sets and all(i < len(lad) for i, lad in zip(idx, ladders)):
        px = [lad[i][0] for i, lad in zip(idx, ladders)]
        unit = sum(p + fee_per_share(p, f) for p, f in zip(px, fee_rates))
        if unit >= 1:
            break
        q = min(left)
        sets += q
        profit += q * (1 - unit)
        for k in range(len(legs)):
            left[k] -= q
            if left[k] <= 1e-9:
                idx[k] += 1
                left[k] = ladders[k][idx[k]][1] if idx[k] < len(ladders[k]) else 0
    return sets, profit


def main(min_edge=0.005, max_pages=5):
    evs = active_events(max_pages)
    print(f"active events: {len(evs)}; multi-market: {sum(1 for e in evs if len(e.get('markets') or []) > 1)}")
    cands = []
    for e in evs:
        legs = legs_of(e)
        for r in partition_check(e, legs, min_edge) + ladder_checks(e, legs, min_edge):
            r.update({"event": e["slug"], "title": e.get("title", "")[:60], "vol24": e.get("volume24hr") or 0,
                      "neg_risk": e.get("negRisk"), "_legs": legs})
            cands.append(r)
    print(f"candidates from Gamma best prices (edge > {min_edge}): {len(cands)}")
    if not cands:
        return
    # verify partitions with real books
    rows = []
    for c in sorted(cands, key=lambda x: -x["edge_per_set"])[:60]:
        legs = c.pop("_legs")
        if c["type"] == "partition_buy_yes":
            with ThreadPoolExecutor(8) as ex:
                bks = dict(zip([lg.token_yes for lg in legs], ex.map(book, [lg.token_yes for lg in legs])))
            if any(v is None or not v["asks"] for v in bks.values()):
                c["verified_sets"], c["verified_profit"] = 0, 0
            else:
                s, p = depth_partition_yes(legs, bks, [lg.fee_rate for lg in legs])
                c["verified_sets"], c["verified_profit"] = round(s, 2), round(p, 2)
        rows.append(c)
    df = pd.DataFrame(rows)
    cols = [c for c in ["type", "edge_per_set", "verified_sets", "verified_profit", "legs", "sum_ask", "sum_bid",
                        "vol24", "event", "buy_yes", "buy_no", "yes_ask", "no_price"] if c in df.columns]
    print(df[cols].round(4).to_string())
    print("\nby type:", df.groupby("type").edge_per_set.agg(["size", "max"]).round(4).to_dict())


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.005, int(sys.argv[2]) if len(sys.argv) > 2 else 5)
