import json, time, re
from lib import get
ms = []
for page in range(1, 8):
    d = get(f"https://openapi.opinion.trade/openapi/market?limit=20&page={page}&status=activated")
    if not d or not (d.get("result") or {}).get("list"): break
    ms += d["result"]["list"]; time.sleep(0.2)
def vol24(m):
    try: return float(m.get("volume24h") or 0)
    except Exception: return 0.0
def book(tid):
    for u in (f"https://openapi.opinion.trade/openapi/token/orderbook?token_id={tid}", f"https://proxy.opinion.trade:8443/openapi/token/orderbook?token_id={tid}"):
        d = get(u, tries=1)
        if d and d.get("result"): return d["result"]
    return None
b = book(ms[0]["yesTokenId"]); print("orderbook sample:", json.dumps(b)[:500] if b else None)
print("sum of 24h volume over all", len(ms), "active markets: $", round(sum(vol24(m) for m in ms)))
rows = []
for m in sorted(ms, key=lambda m: -vol24(m))[:25]:
    by = book(m["yesTokenId"]); time.sleep(0.15)
    if not by: continue
    bids = sorted(((float(x["price"]), float(x["size"])) for x in by.get("bids", [])), reverse=True); asks = sorted((float(x["price"]), float(x["size"])) for x in by.get("asks", []))
    rows.append(dict(title=m["marketTitle"], slug=m.get("slug"), v24=round(vol24(m)), cutoff=time.strftime("%Y-%m-%d", time.gmtime(m.get("cutoffAt") or 0)), bid=bids[0] if bids else None, ask=asks[0] if asks else None, labels=m.get("labels")))
json.dump(rows, open("opin_top.json", "w"))
for r in rows: print(f"{r['title'][:58]:58} v24 ${r['v24']:6} cutoff {r['cutoff']} yes bid {r['bid']} ask {r['ask']}")
