"""7 days of 1-minute prices and taker trades for candidate rewarded markets.
usage: dl_mk.py CANDS.pkl ACTIVE.json OUT.jsonl [DAYS=7] [THREADS=5]
line = {"cid", "t0", "t1", "mid": [[t, p], ...] (YES token), "trades": [[ts, yes_price, size, taker_buys_yes(0/1)], ...]}"""
import json, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
import requests, pandas as pd
tl = threading.local()
def get(url, params):
    s = getattr(tl, "s", None)
    if s is None: s = tl.s = requests.Session()
    for k in range(6):
        try:
            r = s.get(url, params=params, timeout=60)
            if r.status_code == 200: return r.json()
            if r.status_code in (429, 503): time.sleep(2 + 3 * k); continue
            return None
        except Exception:
            pass
        time.sleep(1 + k)
    return None
k = pd.read_pickle(sys.argv[1]); act = {x["condition_id"]: x for x in json.load(open(sys.argv[2]))}
DAYS = float(sys.argv[4]) if len(sys.argv) > 4 else 7; TH = int(sys.argv[5]) if len(sys.argv) > 5 else 5
t1 = int(time.time()); t0 = t1 - int(DAYS * 86400)
a = k[(k.r2 >= 0.5) & (k.spread <= 0.10)]
b = k[(k.r2 >= 0.5) & (k.spread > 0.10)].sample(300, random_state=3)
sel = pd.concat([a, b])
have = set()
try:
    for line in open(sys.argv[3]): have.add(json.loads(line)["cid"])
except FileNotFoundError:
    pass
todo = [c for c in sel.cid if c not in have]
print("markets", len(sel), "todo", len(todo), flush=True)
def one(cid):
    m = act[cid]; yes = m["tokens"][0]["token_id"]
    h = get("https://clob.polymarket.com/prices-history", {"market": yes, "startTs": t0, "endTs": t1, "fidelity": 1})
    mid = [[x["t"], x["p"]] for x in ((h or {}).get("history") or [])]
    rows, cur = [], None
    for _ in range(20):
        p = {"condition": cid, "limit": 500, "taker_only": "true", "start": t0}
        if cur: p["cursor"] = cur
        j = get("https://data-api.polymarket.com/v2/trades", p)
        d = (j or {}).get("data") or []
        rows += d; pg = (j or {}).get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur: break
    tr = []
    for r in rows:
        isyes = r.get("token_id") == yes
        p = float(r["price"]); py = p if isyes else 1 - p
        buys_yes = (r["side"] == "BUY") == isyes
        tr.append([r["timestamp"], round(py, 5), float(r["size"]), 1 if buys_yes else 0])
    tr.sort()
    return {"cid": cid, "t0": t0, "t1": t1, "mid": mid, "trades": tr}
n = 0
with open(sys.argv[3], "a") as f, ThreadPoolExecutor(TH) as ex:
    for rec in ex.map(one, todo):
        f.write(json.dumps(rec) + "\n"); n += 1
        if n % 100 == 0: f.flush(); print(n, "done", flush=True)
print("done", n)
