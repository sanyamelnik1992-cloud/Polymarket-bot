"""All public taker trades of every window in windows.json (data-api v2, newest first, paged): -> trades.json
slug -> [[ts, side B/S, outcome, size, price, wallet]] (the wallet as given: public data)."""
import json, subprocess, time, os
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(5):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return {}
def one(w):
    rows, cur = [], None
    for _ in range(40):
        u = f"https://data-api.polymarket.com/v2/trades?condition={w['cid']}&limit=500&taker_only=true" + (f"&cursor={cur}" if cur else "")
        j = get(u); d = j.get("data") or []
        rows += [[int(x["timestamp"]), x["side"][0], x["outcome"], float(x["size"]), float(x["price"]), x["proxy_wallet"]] for x in d]
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or int(d[-1]["timestamp"]) < w["start"]: break
    return w["slug"], [r for r in rows if w["start"] - 5 <= r[0] <= w["end"] + 5]
W = json.load(open("windows.json"))
out = "trades.json"
have = json.load(open(out)) if os.path.exists(out) else {}
todo = [w for w in W if w["slug"] not in have]
print("todo", len(todo), flush=True)
done = 0
with ThreadPoolExecutor(16) as ex:
    for s, rows in ex.map(one, todo):
        have[s] = rows; done += 1
        if done % 500 == 0:
            print("done", done, time.strftime("%H:%M:%S"), flush=True)
            json.dump(have, open(out, "w"))
json.dump(have, open(out, "w"))
print("windows", len(have), "trades", sum(len(v) for v in have.values()))
