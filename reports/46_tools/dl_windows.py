"""Download every 5m window of one asset: Gamma outcome + all trades (makers included), compact JSON lines.
usage: dl_windows.py ASSET FROM_TS TO_TS OUT.jsonl
line = {"ws", "cond", "winner", "ptb", "trades": [[ts, wallet, side, outcome, size, price, is_taker], ...]}"""
import json, subprocess, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor

asset, t0, t1, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]

def get(url):
    for k in range(4):
        r = subprocess.run(["curl", "-s", "--max-time", "40", url], capture_output=True, text=True)
        try:
            return json.loads(r.stdout)
        except ValueError:
            time.sleep(1 + k)
    return None

def window(ws):
    slug = f"{asset}-updown-5m-{ws}"
    m = get(f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true") or []
    if not m:
        return None
    m = m[0]
    p = json.loads(m.get("outcomePrices") or "[0,0]")
    winner = "Up" if float(p[0]) > 0.9 else "Down" if float(p[1]) > 0.9 else None
    rows, cursor = [], None
    for _ in range(80):
        q = f"condition={m['conditionId']}&limit=500&taker_only=false" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        d = get(f"https://data-api.polymarket.com/v2/trades?{q}")
        if not d or "data" not in d:
            break
        rows += d["data"]
        pg = d.get("pagination") or {}
        cursor = pg.get("next_cursor")
        if not pg.get("has_more") or not cursor:
            break
    groups = {}
    for r in rows:
        groups.setdefault(r["transaction_hash"], []).append(r)
    trades = []
    for h, rs in groups.items():
        tot = sum(float(x["size"]) for x in rs)
        tk = next((x for x in rs if abs(2 * float(x["size"]) - tot) < 1e-6), rs[0])
        for x in rs:
            trades.append([x["timestamp"], x["proxy_wallet"], x["side"], x["outcome"], float(x["size"]), float(x["price"]),
                           1 if x is tk else 0])
    trades.sort(key=lambda t: t[0])
    return {"ws": ws, "cond": m["conditionId"], "winner": winner, "trades": trades}

starts = list(range(t0 - t0 % 300, t1, 300))
with ThreadPoolExecutor(4) as ex, open(out, "w") as f:
    n = 0
    for res in ex.map(window, starts):
        if res:
            f.write(json.dumps(res) + "\n"); n += 1
print(out, "windows", n, "of", len(starts))
