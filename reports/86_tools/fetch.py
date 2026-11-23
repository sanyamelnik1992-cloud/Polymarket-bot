"""Public taker trades of the 5m BTC/ETH/SOL/XRP windows of a time range (data-api v2), cached in trades.json."""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

def get(u):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research-script"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == 3:
                raise
            time.sleep(1 + i)

def window(asset, start):
    slug = f"{asset}-updown-5m-{start}"
    ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not ev:
        return None
    m = ev[0]["markets"][0]
    toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
    outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    pr = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
    win = None
    if m.get("closed") and pr:
        win = outs[0] if float(pr[0]) > 0.5 else outs[1]
    rows, cur = [], None
    for _ in range(40):
        u = f"https://data-api.polymarket.com/v2/trades?condition={m['conditionId']}&limit=500&taker_only=true" + (f"&cursor={cur}" if cur else "")
        j = get(u)
        d = j.get("data") or []
        rows += [[int(x["timestamp"]), x["side"], x["outcome"], float(x["size"]), float(x["price"]), x["proxy_wallet"]] for x in d]
        pg = j.get("pagination") or {}
        cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or int(d[-1]["timestamp"]) < start:
            break
    return {"slug": slug, "asset": asset, "start": start, "winner": win, "trades": rows}

t0, t1 = int(sys.argv[1]), int(sys.argv[2])
starts = list(range(t0 - t0 % 300, t1, 300))
out = "trades.json"
have = json.load(open(out)) if os.path.exists(out) else {}
todo = [(a, s) for s in starts for a in ("btc", "eth", "sol", "xrp") if f"{a}-updown-5m-{s}" not in have or have[f"{a}-updown-5m-{s}"]["winner"] is None]
print("todo", len(todo), flush=True)
with ThreadPoolExecutor(12) as ex:
    for r in ex.map(lambda x: window(*x), todo):
        if r:
            have[r["slug"]] = r
json.dump(have, open(out, "w"))
print("windows", len(have))
