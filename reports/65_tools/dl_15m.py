"""Closing-minute tape of every 15m window (reports/53_tools/dl_lastmin.py for the 15-minute markets) of one asset (makers included), newest-first paging stopped at T+CUT.
usage: dl_lastmin.py ASSET FROM_TS TO_TS OUT.jsonl [THREADS]
line = {"ws", "cond", "winner", "vol", "trades": [[ts, wallet, side, outcome, size, price, is_taker, tx8], ...]}  (ts >= ws + CUT)"""
import json, sys, threading, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests

asset, t0, t1, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
threads = int(sys.argv[5]) if len(sys.argv) > 5 else 3
CUT = 825                        # 15m: the closing minute is [ws + 840, ws + 900); 15 s of margin
tl = threading.local()


def get(url):
    s = getattr(tl, "s", None)
    if s is None:
        s = tl.s = requests.Session()
    for k in range(6):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 2 * k)
                continue
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5 + k)
    return None


def window(ws):
    m = get(f"https://gamma-api.polymarket.com/markets?slug={asset}-updown-15m-{ws}&closed=true") or []
    if not m:
        return None
    m = m[0]
    p = json.loads(m.get("outcomePrices") or "[0,0]")
    winner = "Up" if float(p[0]) > 0.9 else "Down" if float(p[1]) > 0.9 else None
    rows, cursor = [], None
    for _ in range(60):
        q = f"condition={m['conditionId']}&limit=500&taker_only=false" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        d = get(f"https://data-api.polymarket.com/v2/trades?{q}")
        if not d or "data" not in d:
            return None
        rows += d["data"]
        pg = d.get("pagination") or {}
        cursor = pg.get("next_cursor")
        if not pg.get("has_more") or not cursor or (d["data"] and d["data"][-1]["timestamp"] < ws + CUT - 3):
            break
    groups = {}
    for r in rows:
        groups.setdefault(r["transaction_hash"], []).append(r)
    trades = []
    for h, rs in groups.items():
        if rs[0]["timestamp"] < ws + CUT:
            continue
        tot = sum(float(x["size"]) for x in rs)
        tk = next((x for x in rs if abs(2 * float(x["size"]) - tot) < 1e-6), rs[0])
        for x in rs:
            trades.append([x["timestamp"], x["proxy_wallet"][:12], x["side"], x["outcome"], float(x["size"]), float(x["price"]),
                           1 if x is tk else 0, h[2:10]])
    trades.sort(key=lambda t: t[0])
    return {"ws": ws, "cond": m["conditionId"], "winner": winner, "vol": float(m.get("volume") or 0), "trades": trades}


have = set()
try:
    for line in open(out):
        have.add(json.loads(line)["ws"])
except FileNotFoundError:
    pass
starts = [s for s in range(t0 - t0 % 900, t1, 900) if s not in have]
n = 0
with ThreadPoolExecutor(threads) as ex, open(out, "a") as f:
    for res in ex.map(window, starts):
        if res:
            f.write(json.dumps(res, separators=(",", ":")) + "\n")
            n += 1
            if n % 100 == 0:
                f.flush()
                print(asset, n, "of", len(starts), flush=True)
print(out, "windows", n, "of", len(starts), flush=True)
