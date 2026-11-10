"""Sample the CLOB books of the current 5m windows (btc, eth, sol, xrp; both tokens) during the closing minute.
REST only (POST /books), ~3 Hz. One JSON line per poll: {"t": local s, "ws": window start, "books": {asset: {"U"/"D": {...}}}}.
usage: book_sampler.py OUT.jsonl MINUTES"""
import os
import json, sys, time
import requests

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, ROOT)
from pmbot.gamma import fetch_event, parse_event, slug_for  # noqa: E402

out, minutes = sys.argv[1], float(sys.argv[2])
ASSETS = ["btc", "eth", "sol", "xrp"]
S = requests.Session()
meta = {}          # ws -> {asset: (token_up, token_down)}
t_end = time.time() + minutes * 60


def tokens(ws):
    m = meta.setdefault(ws, {})
    for a in ASSETS:
        if a in m:
            continue
        try:
            e = fetch_event(slug_for(a, ws, "5m"))
            if e:
                p = parse_event(e)
                m[a] = (p["token_up"], p["token_down"])
        except Exception as ex:  # noqa: BLE001
            print("gamma", a, ws, ex, flush=True)
    for old in [k for k in meta if k < ws - 900]:
        meta.pop(old, None)
    return m


def top(levels, n, reverse):
    xs = sorted(((float(x["price"]), float(x["size"])) for x in levels), reverse=reverse)
    return xs[:n]


with open(out, "a") as f:
    n = 0
    while time.time() < t_end:
        now = time.time()
        ws = int(now // 300) * 300
        rel = now - ws
        if rel < 200:
            time.sleep(min(200 - rel, 5))
            continue
        m = tokens(ws)
        if rel < 228 or not m:
            time.sleep(0.5)
            continue
        ids, who = [], {}
        for a, (u, d) in m.items():
            ids += [{"token_id": u}, {"token_id": d}]
            who[u], who[d] = (a, "U"), (a, "D")
        t0 = time.time()
        try:
            r = S.post("https://clob.polymarket.com/books", json=ids, timeout=4)
            data = r.json()
        except Exception as ex:  # noqa: BLE001
            print("books", ex, flush=True)
            time.sleep(0.3)
            continue
        t1 = time.time()
        rec = {"t": round(t0, 3), "dt": round(t1 - t0, 3), "ws": ws, "books": {}}
        for b in data if isinstance(data, list) else []:
            a, side = who.get(b.get("asset_id"), (None, None))
            if a is None:
                continue
            rec["books"].setdefault(a, {})[side] = {"ts": int(b.get("timestamp") or 0), "ltp": b.get("last_trade_price"),
                                                    "tick": b.get("tick_size"), "asks": top(b.get("asks") or [], 8, False),
                                                    "bids": top(b.get("bids") or [], 4, True)}
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        n += 1
        if n % 50 == 0:
            f.flush()
        time.sleep(max(0.0, 0.3 - (time.time() - t0)))
print("done", n)
