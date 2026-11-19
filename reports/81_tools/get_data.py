"""Gamma outcomes + public taker trades near the end of every 5m / 15m crypto Up/Down window, 1-17 Nov (public data)."""
import json, os, sys, time, calendar, collections
from concurrent.futures import ThreadPoolExecutor
import requests
S = requests.Session(); S.headers["User-Agent"] = "research-client"
def get(u, tries=5):
    for k in range(tries):
        try:
            r = S.get(u, timeout=40)
            if r.status_code == 200: return r.json()
        except Exception: pass
        time.sleep(1 + 2 * k)
    return None
t_from, t_to = calendar.timegm((2026, 11, 1, 0, 0, 0)), int(sys.argv[1]) if len(sys.argv) > 1 else calendar.timegm((2026, 11, 17, 0, 0, 0))
W = []
for a in ("btc", "eth", "sol", "xrp"):
    for dur, tag in ((300, "5m"), (900, "15m")):
        for st in range(t_from, t_to - dur + 1, dur):
            W.append((f"{a}-updown-{tag}-{st}", a, tag, st, st + dur))
out_w = "windows_all.json"
have_w = {w["slug"]: w for w in json.load(open(out_w))} if os.path.exists(out_w) else {}
def batch(ws):
    q = "&".join(f"slug={w[0]}" for w in ws)
    res = {}
    for m in get(f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100") or []:
        op = json.loads(m.get("outcomePrices") or "[]"); outs = json.loads(m.get("outcomes") or "[]")
        if "1" in op: res[m["slug"]] = dict(win=outs[op.index("1")], cid=m["conditionId"], outs=outs)
    return res
todo = [w for w in W if w[0] not in have_w]
O = {}
with ThreadPoolExecutor(8) as ex:
    for r in ex.map(batch, [todo[i:i + 40] for i in range(0, len(todo), 40)]): O.update(r)
for s, a, k, st, en in todo:
    if s in O: have_w[s] = dict(slug=s, asset=a, kind=k, start=st, end=en, **O[s])
json.dump(list(have_w.values()), open(out_w, "w"))
print("windows", len(W), "with outcome", len(have_w), collections.Counter(w["kind"] for w in have_w.values()), flush=True)
LOOK = {"5m": 240, "15m": 600}
def one(w):
    rows, cur = [], None
    for _ in range(8):
        u = f"https://data-api.polymarket.com/v2/trades?condition={w['cid']}&limit=500&taker_only=true"
        if cur: u += f"&cursor={cur}"
        j = get(u) or {}; d = j.get("data") or []
        rows += [[int(x["timestamp"]), x["side"][0], x["outcome"], float(x["size"]), float(x["price"]), x["proxy_wallet"][:10]] for x in d]
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or int(d[-1]["timestamp"]) < w["end"] - LOOK[w["kind"]]: break
    return w["slug"], [r for r in rows if r[0] >= w["end"] - LOOK[w["kind"]]]
out = "mtrades_all.json"
have = json.load(open(out)) if os.path.exists(out) else {}
todo = sorted([w for w in have_w.values() if w["slug"] not in have], key=lambda w: (w["kind"] != "15m", w["start"]))
print("trades todo", len(todo), flush=True)
done = 0
with ThreadPoolExecutor(16) as ex:
    for s, rows in ex.map(one, todo):
        have[s] = rows; done += 1
        if done % 1000 == 0:
            print("done", done, time.strftime("%H:%M:%S"), flush=True); json.dump(have, open(out, "w"))
json.dump(have, open(out, "w"))
print("windows", len(have), "trades", sum(len(v) for v in have.values()), flush=True)
