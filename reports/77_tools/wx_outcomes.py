"""Resolved weather markets of a trades file, from Gamma (closed=true).
usage: python wx_outcomes.py trades_weather.json wx_markets.json"""
import json, subprocess, sys, re, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from catlib import cat
tr = json.load(open(sys.argv[1])); out = sys.argv[2]
try: have = json.load(open(out))
except Exception: have = {}
slugs = sorted({r["s"] for rows in tr.values() for r in rows if cat(r["s"], r["title"]) == "weather"} - set(have))
def get(batch):
    q = "&".join(f"slug={s}" for s in batch)
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", f"https://gamma-api.polymarket.com/markets?{q}&limit=100&closed=true"], capture_output=True, text=True).stdout
        try:
            res = {}
            for m in json.loads(r):
                op = json.loads(m.get("outcomePrices") or "[]")
                res[m["slug"]] = dict(closed=bool(m.get("closed")), yes=float(op[0]) if op else None, end=m.get("endDate"),
                                      outcomes=m.get("outcomes"), q=m.get("question"))
            return res
        except Exception:
            time.sleep(1)
    return {}
with ThreadPoolExecutor(8) as ex:
    for d in ex.map(get, [slugs[i:i + 40] for i in range(0, len(slugs), 40)]):
        have.update(d)
json.dump(have, open(out, "w"))
print("weather slugs", len(slugs), "known", len(have), "closed", sum(1 for v in have.values() if v["closed"]))
