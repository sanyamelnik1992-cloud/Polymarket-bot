"""Real (Gamma) outcomes for the windows our own TWAP rule calls close. The markets files of prep_day.py hold OUR rule's
winner ([T-62, T-3] vs [T+238, T+297]); the exchange averages one second later in about a third of the windows
(window_shift.py), which flips the outcome when the margin is a fraction of a bp (3 of 1616 windows on 8-10 Nov).
Writes DATA/gamma_close.parquet (asset, window_start, g_winner, g_ptb, g_final); sim.load() applies it.

usage: python reports/52_tools/gamma_outcomes.py DATA_DIR PERIOD [PERIOD ...]     (needs DATA_DIR/PERIOD_markets.parquet)
"""
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

import pandas as pd

MARGIN_BP = 1.5


def get(r):
    url = f"https://gamma-api.polymarket.com/events?slug={r.asset}-updown-5m-{int(r.window_start)}"
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pmbot-research"})
            e = json.load(urllib.request.urlopen(req, timeout=15))[0]
            m = e["markets"][0]
            pr = [float(x) for x in json.loads(m["outcomePrices"])]
            oc = json.loads(m["outcomes"])
            w = oc[pr.index(max(pr))] if m.get("closed") and max(pr) > 0.99 else None
            md = e.get("eventMetadata") or {}
            return r.asset, int(r.window_start), w, md.get("priceToBeat"), md.get("finalPrice")
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return r.asset, int(r.window_start), None, None, None


if __name__ == "__main__":
    data, periods = sys.argv[1], sys.argv[2:]
    d = pd.concat([pd.read_parquet(os.path.join(data, f"{p}_markets.parquet")).assign(period=p) for p in periods])
    d = d.drop_duplicates(["asset", "window_start"])
    d["margin_bp"] = (d.final / d.price_to_beat - 1).abs() * 1e4
    todo = d[d.margin_bp < MARGIN_BP]
    with cf.ThreadPoolExecutor(6) as ex:
        res = list(ex.map(get, list(todo.itertuples())))
    g = pd.DataFrame(res, columns=["asset", "window_start", "g_winner", "g_ptb", "g_final"])
    g.to_parquet(os.path.join(data, "gamma_close.parquet"))
    x = todo.merge(g, on=["asset", "window_start"])
    mm = x[x.g_winner.notna() & (x.g_winner != x.winner)]
    print(f"windows {len(d)}, close ones fetched {int(x.g_winner.notna().sum())}/{len(todo)}, our rule differs in {len(mm)}")
    print(mm[["period", "asset", "window_start", "winner", "g_winner", "margin_bp"]].round(3).to_string())
