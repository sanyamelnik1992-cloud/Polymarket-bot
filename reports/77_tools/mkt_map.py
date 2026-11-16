"""Market-wide taker edge on 5m / 15m Up/Down windows: every taker trade of N recent windows (public data),
edge = payoff - price - taker fee, by time in the window and price. Not survivor-selected."""
import json, subprocess, time, collections, random
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1)
    return {}
now = int(time.time())
wins = []
for dur, tag in ((300, "5m"), (900, "15m")):
    last = (now // dur) * dur - 2 * dur
    for i in range(0, 48 * 3600 // dur):
        st = last - i * dur
        for a in ("btc", "eth", "sol", "xrp"):
            wins.append((f"{a}-updown-{tag}-{st}", dur, st))
random.seed(7); random.shuffle(wins)
wins = [w for w in wins if w[1] == 300][:400] + [w for w in wins if w[1] == 900][:200]
def outcome(batch):
    q = "&".join(f"slug={s}" for s, _, _ in batch)
    d = get(f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100")
    res = {}
    for m in d if isinstance(d, list) else []:
        op = json.loads(m.get("outcomePrices") or "[]")
        if op and op[0] in ("1", "0"): res[m["slug"]] = ("Up" if op[0] == "1" else "Down", m["conditionId"])
    return res
oc = {}
with ThreadPoolExecutor(8) as ex:
    for r in ex.map(outcome, [wins[i:i + 40] for i in range(0, len(wins), 40)]): oc.update(r)
print("windows", len(wins), "resolved", len(oc))
def trades(item):
    s, (win, cid) = item
    rows, cur = [], None
    for _ in range(6):
        u = f"https://data-api.polymarket.com/v2/trades?condition={cid}&limit=500&taker_only=true"
        if cur: u += f"&cursor={cur}"
        j = get(u); d = j.get("data") or []; rows += d
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur: break
    return s, win, rows
out = {}
with ThreadPoolExecutor(8) as ex:
    for s, win, rows in ex.map(trades, list(oc.items())):
        out[s] = dict(win=win, rows=[dict(side=r["side"], o=r["outcome"], sz=float(r["size"]), px=float(r["price"]), t=int(r["timestamp"]), w=r["proxy_wallet"]) for r in rows])
json.dump(out, open("mkt_taker.json", "w"))
print("taker trades", sum(len(v["rows"]) for v in out.values()))
