"""Gamma outcomes of all 5m / 15m / 1h crypto Up/Down windows 9-16 Nov (public): slug -> (winner, conditionId, end)."""
import json, subprocess, time, calendar, os
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return []
A = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "xrp": "xrp"}
t_from, t_to = calendar.timegm((2026, 11, 9, 0, 0, 0)), calendar.timegm((2026, 11, 17, 0, 0, 0))
W = []  # (slug, asset, kind, start, end)
for a in A:
    for dur, tag in ((300, "5m"), (900, "15m")):
        for st in range(t_from, t_to, dur):
            W.append((f"{a}-updown-{tag}-{st}", a, tag, st, st + dur))
    for st in range(t_from, t_to, 3600):
        et = time.gmtime(st - 5 * 3600)                     # EST (UTC-5) in November
        h = et.tm_hour; hh = f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}"
        W.append((f"{A[a]}-up-or-down-november-{et.tm_mday}-2026-{hh}-et", a, "1h", st, st + 3600))
def batch(ws):
    q = "&".join(f"slug={w[0]}" for w in ws)
    res = {}
    for m in get(f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100") or []:
        op = json.loads(m.get("outcomePrices") or "[]"); outs = json.loads(m.get("outcomes") or "[]")
        if "1" in op: res[m["slug"]] = dict(win=outs[op.index("1")], cid=m["conditionId"], toks=json.loads(m.get("clobTokenIds") or "[]"), outs=outs)
    return res
O = {}
with ThreadPoolExecutor(8) as ex:
    for r in ex.map(batch, [W[i:i + 40] for i in range(0, len(W), 40)]): O.update(r)
out = [dict(slug=s, asset=a, kind=k, start=st, end=en, **O[s]) for s, a, k, st, en in W if s in O]
json.dump(out, open("windows.json", "w"))
import collections
print("windows", len(W), "with outcome", len(out), collections.Counter(w["kind"] for w in out))
