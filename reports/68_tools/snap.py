"""Compact snapshot of the active reward markets (stops at the first page without an active pool).
usage: snap.py OUT.json"""
import json, sys, time
import requests
S = requests.Session()
today = time.strftime("%Y-%m-%d", time.gmtime())
rows, cur = {}, None
for i in range(60):
    p = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500}
    if cur: p["next_cursor"] = cur
    j = None
    for k in range(5):
        try:
            r = S.get("https://clob.polymarket.com/rewards/markets/multi", params=p, timeout=90)
            if r.status_code == 200: j = r.json(); break
        except Exception as e:
            pass
        time.sleep(2 + 2 * k)
    if j is None: break
    d = j.get("data") or []; n_act = 0
    for m in d:
        rate = sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or [])
                   if (c.get("start_date") or "0000") <= today <= (c.get("end_date") or "9999"))
        if rate <= 0: continue
        n_act += 1
        rows[m["condition_id"]] = dict(rate=rate, comp=float(m.get("market_competitiveness") or 0), spread=float(m.get("spread") or 0),
                                       vol=float(m.get("volume_24hr") or 0), p=float((m.get("tokens") or [{}])[0].get("price") or 0),
                                       ms=float(m.get("rewards_min_size") or 0), v=float(m.get("rewards_max_spread") or 0))
    cur = j.get("next_cursor")
    if not d or n_act == 0 or cur in (None, "LTE="): break
json.dump({"ts": time.time(), "rows": rows}, open(sys.argv[1], "w"))
print("active markets", len(rows), "pages", i + 1, "pool", round(sum(r["rate"] for r in rows.values())))
