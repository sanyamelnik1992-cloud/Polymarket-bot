"""One wallet's trades in Up/Down markets with outcomes from Gamma: per-market entries and daily PnL."""
import json, collections, sys, time, subprocess
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return []
src, pref, out = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(src))
w = [k for k in d if k.startswith(pref)][0]
rows = sorted(d[w], key=lambda r: r["t"])
slugs = sorted({r["s"] for r in rows})
def oc(batch):
    q = "&".join(f"slug={s}" for s in batch)
    res = {}
    for m in get(f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100") or []:
        op = json.loads(m.get("outcomePrices") or "[]"); outs = json.loads(m.get("outcomes") or "[]")
        win = outs[op.index("1")] if "1" in op else None
        res[m["slug"]] = dict(win=win, closed=m.get("closed"), end=m.get("endDate"), start=m.get("eventStartTime") or m.get("startDate"))
    return res
O = {}
with ThreadPoolExecutor(6) as ex:
    for r in ex.map(oc, [slugs[i:i + 40] for i in range(0, len(slugs), 40)]): O.update(r)
json.dump(dict(wallet=w, rows=rows, oc=O), open(out, "w"))
print("wallet", w[:10], "trades", len(rows), "markets", len(slugs), "with outcome", sum(1 for s in slugs if O.get(s, {}).get("win")))
print("sides", collections.Counter(r["side"] for r in rows), "kinds", collections.Counter(r["s"].split("-")[0] + "-" + r["s"].split("-")[2] for r in rows if "updown" in r["s"]))
print("non-updown", collections.Counter(r["s"] for r in rows if "updown" not in r["s"]).most_common(5))
