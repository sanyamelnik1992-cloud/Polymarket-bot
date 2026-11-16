"""All trades (taker side) of every bucket of the closed temperature events: time, side, outcome, price, size.
usage: python collect_trades.py   (trades.json: every taker trade of every bucket of the closed events in events.json)"""
import json, subprocess, time, sys
from concurrent.futures import ThreadPoolExecutor
ev = json.load(open("events.json"))
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return {}
jobs = [(slug, m["cid"]) for slug, e in ev.items() if e["closed"] and e["desc_ok"] and e["station"] for m in e["markets"]]
def fetch(job):
    slug, cid = job; rows, cur = [], None
    for _ in range(10):
        u = f"https://data-api.polymarket.com/v2/trades?condition={cid}&limit=500&taker_only=true" + (f"&cursor={cur}" if cur else "")
        j = get(u); d = j.get("data") or []; rows += d
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur: break
    return cid, [dict(t=int(r["timestamp"]), side=r["side"], o=r["outcome"], px=float(r["price"]), sz=float(r["size"])) for r in rows]
out = {}
with ThreadPoolExecutor(8) as ex:
    for cid, rows in ex.map(fetch, jobs): out[cid] = rows
json.dump(out, open("trades.json", "w"))
print("markets", len(jobs), "trades", sum(len(v) for v in out.values()), "capped(>=5000)", sum(len(v) >= 5000 for v in out.values()))
