"""The real entries of the live lock bot from the public Data API, by price and entry second, with Gamma outcomes.
The second is the trade's chain timestamp minus the window start: the decision was 1-3 s earlier.
usage: live_entries.py WALLET SINCE_UNIX_TS [CUT_SECOND=260]   (the wallet address stays out of the repository)"""
import json, sys, time
import requests
w, since = sys.argv[1], int(sys.argv[2])
cut = int(sys.argv[3]) if len(sys.argv) > 3 else 260
S = requests.Session()
out, cur = [], None
while True:
    p = {"user": w, "limit": 100, "taker_only": "false"}
    if cur: p["cursor"] = cur
    j = S.get("https://data-api.polymarket.com/v2/trades", params=p, timeout=30).json()
    d = j.get("data") or []
    out += d; cur = j.get("next_cursor")
    if not d or not cur or d[-1]["timestamp"] < since: break
rows, res = [], {}
for r in out:
    s = r["slug"]
    if "updown-5m" not in s or r["side"] != "BUY" or r["timestamp"] < since: continue
    rel = r["timestamp"] - int(s.rsplit("-", 1)[1])
    if rel < 230: continue                                   # not a lock entry
    if s not in res:
        m = S.get("https://gamma-api.polymarket.com/markets", params={"slug": s, "closed": "true"}, timeout=30).json()
        res[s] = json.loads(m[0]["outcomePrices"]) if m and m[0].get("outcomePrices") else None
    if res[s]:
        rows.append((r["timestamp"], s.split("-")[0], rel, round(r["price"], 3), r["size"], (res[s][0] == "1") == (r["outcome"] == "Up")))
def agg(name, f):
    xs = [x for x in rows if f(x)]
    pnl = sum((x[4] * (1 - x[3]) if x[5] else -x[4] * x[3]) - 0.07 * x[3] * (1 - x[3]) * x[4] for x in xs)
    print(f"{name:34s} entries {len(xs):3d}  lost {sum(1 for x in xs if not x[5]):2d}  {pnl:+6.2f}$")
agg("all", lambda x: True)
agg(f"price <= 0.91, before second {cut}", lambda x: x[3] <= 0.911 and x[2] < cut)
agg(f"price <= 0.91, from second {cut}", lambda x: x[3] <= 0.911 and x[2] >= cut)
agg("price > 0.91", lambda x: x[3] > 0.911)
agg("all but the early cheap ones", lambda x: not (x[3] <= 0.911 and x[2] < cut))
for x in sorted(rows):
    if not x[5]: print("LOSS", time.strftime("%d %b %H:%M:%S", time.gmtime(x[0])), x[1], "second", x[2], "price", x[3])
