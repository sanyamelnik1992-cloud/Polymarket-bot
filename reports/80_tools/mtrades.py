"""Taker trades near the end of every window (public data-api v2, newest first): enough pages to reach end - LOOKBACK."""
import json, subprocess, time, os, sys
from concurrent.futures import ThreadPoolExecutor
LOOK = {"5m": 240, "15m": 600, "1h": 1200}
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return {}
def one(w):
    rows, cur = [], None
    for _ in range(6):
        u = f"https://data-api.polymarket.com/v2/trades?condition={w['cid']}&limit=500&taker_only=true"
        if cur: u += f"&cursor={cur}"
        j = get(u); d = j.get("data") or []
        rows += [[int(x["timestamp"]), x["side"][0], x["outcome"], float(x["size"]), float(x["price"]), x["proxy_wallet"][:10]] for x in d]
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or int(d[-1]["timestamp"]) < w["end"] - LOOK[w["kind"]]: break
    return w["slug"], [r for r in rows if r[0] >= w["end"] - LOOK[w["kind"]]]
W = json.load(open("windows.json"))
out = "mtrades.json"
have = json.load(open(out)) if os.path.exists(out) else {}
todo = sorted([w for w in W if w["slug"] not in have], key=lambda w: {"15m": 0, "1h": 1, "5m": 2}[w["kind"]])
print("todo", len(todo), flush=True)
done = 0
with ThreadPoolExecutor(16) as ex:
    for s, rows in ex.map(one, todo):
        have[s] = rows; done += 1
        if done % 1000 == 0:
            print("done", done, time.strftime("%H:%M:%S"), flush=True)
            json.dump(have, open(out, "w"))
json.dump(have, open(out, "w"))
print("windows", len(have), "trades", sum(len(v) for v in have.values()))
