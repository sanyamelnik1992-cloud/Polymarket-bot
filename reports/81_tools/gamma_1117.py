"""Gamma outcomes of the 5m windows of 17 Nov so far (public). Writes w1117.json."""
import json, time, calendar, requests
S = requests.Session(); S.headers["User-Agent"] = "research-client"
t0, t1 = calendar.timegm((2026, 11, 17, 0, 0, 0)), int(time.time()) - 600
todo = [(f"{a}-updown-5m-{st}", a, "5m", st, st + 300) for a in ("btc", "eth", "sol", "xrp") for st in range(t0, t1 - 300, 300)]
out = []
for i in range(0, len(todo), 40):
    ch = todo[i:i + 40]
    r = S.get("https://gamma-api.polymarket.com/markets?" + "&".join(f"slug={w[0]}" for w in ch) + "&closed=true&limit=100", timeout=40).json()
    got = {}
    for m in r:
        op = json.loads(m.get("outcomePrices") or "[]"); outs = json.loads(m.get("outcomes") or "[]")
        if "1" in op: got[m["slug"]] = dict(win=outs[op.index("1")], cid=m["conditionId"], outs=outs)
    out += [dict(slug=s, asset=a, kind=k, start=st, end=en, **got[s]) for s, a, k, st, en in ch if s in got]
json.dump(out, open("w1117.json", "w")); print("windows", len(todo), "with outcome", len(out))
