"""Maker wallets of a stratified sample of rewarded markets (public tape, makers = non-taker legs of each transaction).
usage: pop_makers.py OUT.json [PAGES=2]"""
import json, sys, time, collections, threading
from concurrent.futures import ThreadPoolExecutor
import requests
import pandas as pd
import numpy as np
tl = threading.local()
def get(url, params):
    s = getattr(tl, "s", None)
    if s is None: s = tl.s = requests.Session()
    for k in range(6):
        try:
            r = s.get(url, params=params, timeout=40)
            if r.status_code == 200: return r.json()
            if r.status_code in (429, 503): time.sleep(2 + 3 * k); continue
            return None
        except Exception:
            pass
        time.sleep(1 + k)
    return None
PAGES = int(sys.argv[2]) if len(sys.argv) > 2 else 2
d = pd.read_pickle("gross.pkl")
d = d[(d.vol > 0) & (d.days > 0)]
rng = np.random.default_rng(7)
def pick(df, n): return df.sample(min(n, len(df)), random_state=7)
sample = pd.concat([d[d.rate >= 200].sort_values("rate", ascending=False).head(70),
                    pick(d[(d.rate >= 50) & (d.rate < 200)], 120), pick(d[(d.rate >= 20) & (d.rate < 50)], 150),
                    pick(d[(d.rate >= 5) & (d.rate < 20)], 150), pick(d[(d.rate >= 1) & (d.rate < 5)], 110)]).drop_duplicates("cid")
print("markets", len(sample), flush=True)
def one(m):
    rows, cur = [], None
    for _ in range(PAGES):
        p = {"condition": m.cid, "limit": 500, "taker_only": "false"}
        if cur: p["cursor"] = cur
        j = get("https://data-api.polymarket.com/v2/trades", p)
        dd = (j or {}).get("data") or []
        rows += dd
        pg = (j or {}).get("pagination") or {}
        cur = pg.get("next_cursor")
        if not pg.get("has_more") or not cur: break
    groups = collections.defaultdict(list)
    for r in rows: groups[r["transaction_hash"]].append(r)
    mk = collections.Counter(); vol = collections.Counter()
    for h, rs in groups.items():
        tot = sum(float(x["size"]) for x in rs)
        tk = next((x for x in rs if abs(2 * float(x["size"]) - tot) < 1e-6), None)
        if tk is None: continue
        for x in rs:
            if x is not tk:
                mk[x["proxy_wallet"]] += 1; vol[x["proxy_wallet"]] += float(x["size"]) * float(x["price"])
    span = (rows[0]["timestamp"] - rows[-1]["timestamp"]) / 86400 if rows else 0
    return m.cid, dict(mk), dict(vol), len(rows), span
out = {}
with ThreadPoolExecutor(4) as ex:
    for i, (cid, mk, vol, n, span) in enumerate(ex.map(one, [m for _, m in sample.iterrows()])):
        out[cid] = {"makers": mk, "vol": vol, "rows": n, "span_days": span}
        if i % 50 == 0: print(i, "markets done", flush=True)
allm = collections.Counter(); nm = collections.Counter()
for cid, o in out.items():
    for w, n in o["makers"].items(): allm[w] += n; nm[w] += 1
json.dump({"markets": out, "sample": sample[["cid", "rate", "comp", "vol", "ms", "v", "p", "days", "q"]].to_dict("records")}, open(sys.argv[1], "w"))
print("distinct makers", len(allm), " in >=2 markets:", sum(1 for w in nm if nm[w] >= 2), " in >=5:", sum(1 for w in nm if nm[w] >= 5))
