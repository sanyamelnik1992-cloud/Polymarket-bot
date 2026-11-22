"""Gamma outcomes of the 5m / 15m crypto Up/Down windows 18 Nov 01:00 - 22 Nov 00:00 (public): -> windows.json"""
import json, subprocess, time, calendar, collections
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return []
t_from, t_to = calendar.timegm((2026, 11, 18, 1, 0, 0)), calendar.timegm((2026, 11, 22, 0, 0, 0))
W = []
for a in ("btc", "eth", "sol", "xrp"):
    for dur, tag in ((300, "5m"), (900, "15m")):
        for st in range(t_from, t_to, dur):
            W.append((f"{a}-updown-{tag}-{st}", a, tag, st, st + dur))
def batch(ws):
    q = "&".join(f"slug={w[0]}" for w in ws)
    res = {}
    for m in get(f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100") or []:
        op = json.loads(m.get("outcomePrices") or "[]"); outs = json.loads(m.get("outcomes") or "[]")
        if "1" in op: res[m["slug"]] = dict(win=outs[op.index("1")], cid=m["conditionId"], outs=outs)
    return res
O = {}
with ThreadPoolExecutor(8) as ex:
    for r in ex.map(batch, [W[i:i + 40] for i in range(0, len(W), 40)]): O.update(r)
out = [dict(slug=s, asset=a, kind=k, start=st, end=en, **O[s]) for s, a, k, st, en in W if s in O]
json.dump(out, open("windows.json", "w"))
print("windows", len(W), "with outcome", len(out), collections.Counter(w["kind"] for w in out))
