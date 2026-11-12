"""Download the whole public rewards collection. usage: fetch_rewards.py OUT.json"""
import json, sys, time
import requests
S = requests.Session()
rows, cur = [], None
for i in range(200):
    p = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500}
    if cur: p["next_cursor"] = cur
    for k in range(5):
        try:
            r = S.get("https://clob.polymarket.com/rewards/markets/multi", params=p, timeout=90)
            if r.status_code == 200:
                j = r.json(); break
            print("status", r.status_code, r.text[:200], flush=True)
        except Exception as e:
            print("err", e, flush=True)
        time.sleep(2 + 2 * k)
    else:
        break
    d = j.get("data") or []
    rows += d; cur = j.get("next_cursor")
    if not d or cur in (None, "LTE="): break
json.dump({"ts": time.time(), "rows": rows}, open(sys.argv[1], "w"))
print("rows", len(rows), "pages", i + 1)
