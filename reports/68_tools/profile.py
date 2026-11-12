"""Profile a maker wallet from public data: where it quotes, how big, how often it is filled, what it holds.
usage: profile.py WALLET_PREFIX [DAYS=3]"""
import json, sys, time, collections
import requests, numpy as np, pandas as pd
S = requests.Session()
pref = sys.argv[1]; DAYS = float(sys.argv[2]) if len(sys.argv) > 2 else 3
w = None
for line in open("pnl.jsonl"):
    r = json.loads(line)
    if r["w"].startswith(pref): w = r["w"]; break
assert w, "wallet not in pnl.jsonl"
def get(path, p):
    for k in range(6):
        try:
            r = S.get("https://data-api.polymarket.com" + path, params=p, timeout=60)
            if r.status_code == 200: return r.json()
            if r.status_code in (429, 503): time.sleep(2 + 3 * k); continue
            print("status", r.status_code, r.text[:150]); return None
        except Exception as e:
            time.sleep(1 + k)
def feed(path, p, stop_ts, cap=40000):
    rows, cur = [], None
    while len(rows) < cap:
        q = dict(p); q["limit"] = 500
        if cur: q["cursor"] = cur
        j = get(path, q)
        d = (j or {}).get("data") or []
        rows += d
        pg = (j or {}).get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or d[-1]["timestamp"] < stop_ts: break
    return [r for r in rows if r["timestamp"] >= stop_ts]
now = time.time(); t0 = now - DAYS * 86400
allt = feed("/v2/trades", {"user": w, "taker_only": "false"}, t0)
tak = feed("/v2/trades", {"user": w, "taker_only": "true"}, t0)
tk = {(r["transaction_hash"], r["token_id"]) for r in tak}
mk = [r for r in allt if (r["transaction_hash"], r["token_id"]) not in tk]
print(f"wallet {w[:10]}: last {DAYS:g} days: trades {len(allt)} (maker fills {len(mk)}, taker {len(allt) - len(mk)})")
g = pd.read_pickle("gross.pkl").set_index("cid")
m = pd.DataFrame([dict(cid=r["condition_id"], side=r["side"], size=float(r["size"]), price=float(r["price"]), ts=r["timestamp"], title=r.get("title", ""), outcome=r.get("outcome_index")) for r in mk])
if len(m):
    m["usd"] = m["size"] * m.price
    m = m.join(g[["rate", "comp", "ms", "v", "p", "days", "vol"]], on="cid")
    rw = m[m.rate.notna()]
    print(f" maker fills: {len(m)} in {m.cid.nunique()} markets, ${m.usd.sum():.0f} ({m.usd.sum()/DAYS:.0f}/day); in rewarded markets: {len(rw)} fills, {rw.cid.nunique()} markets, ${rw.usd.sum():.0f}")
    print(" fill size (shares) quantiles 10/25/50/75/90:", np.round(m["size"].quantile([.1, .25, .5, .75, .9]).values, 1), " BUY share:", round((m.side == "BUY").mean(), 2))
    if len(rw):
        rel = rw["size"] / rw.ms
        print(" fill size / rewards min size quantiles 10/25/50/75/90:", np.round(rel.quantile([.1, .25, .5, .75, .9]).values, 2))
        per = rw.groupby("cid").agg(fills=("usd", "size"), usd=("usd", "sum"), rate=("rate", "first"), comp=("comp", "first"), ms=("ms", "first"), p=("p", "first"), days=("days", "first"), vol=("vol", "first"), title=("title", "first"))
        print(" markets with fills by pool: ", {f"{lo}-{hi}": int(((per.rate >= lo) & (per.rate < hi)).sum()) for lo, hi in ((0, 5), (5, 20), (20, 50), (50, 200), (200, 1e9))})
        print(" markets with fills by competitiveness: ", {f"{lo}-{hi}": int(((per.comp >= lo) & (per.comp < hi)).sum()) for lo, hi in ((0, 1), (1, 5), (5, 20), (20, 100), (100, 1e9))})
        print(" fills per market per day median %.2f; price of fills quantiles 10/50/90: %s" % ((per.fills / DAYS).median(), np.round(rw.price.quantile([.1, .5, .9]).values, 3)))
        print(per.sort_values("usd", ascending=False).head(12).assign(title=lambda x: x.title.str[:55]).round(2).to_string())
# positions
pos, cur = [], None
for _ in range(20):
    q = {"user": w, "limit": 500}
    if cur: q["cursor"] = cur
    j = get("/v2/positions", q); d = (j or {}).get("data") or []
    pos += d; pg = (j or {}).get("pagination") or {}; cur = pg.get("next_cursor")
    if not d or not pg.get("has_more") or not cur: break
p = pd.DataFrame([dict(cid=r["condition_id"], size=float(r["current_size"]), avg=float(r["avg_price"]), cur=float(r["current_price"]), val=float(r["current_value"]),
                       pnl=float(r["total_pnl"]), cost=float(r["total_cost_usdc"]), title=r.get("title", "")) for r in pos])
if len(p):
    p = p.join(g[["rate", "ms"]], on="cid")
    print(f" open positions: {len(p)} in {p.cid.nunique()} markets, value ${p.val.sum():.0f}; size quantiles 50/90/99: {np.round(p['size'].quantile([.5, .9, .99]).values, 0)}; "
          f"value per position median ${p.val.median():.1f}, p90 ${p.val.quantile(.9):.1f}; in rewarded markets: {int(p.rate.notna().sum())}")
    both = p.groupby("cid").size(); print(f" markets holding both outcomes: {int((both >= 2).sum())}")
# other activity
act = feed("/v2/activity", {"user": w, "type": "MERGE,SPLIT,REDEEM,REWARD"}, t0, cap=5000)
c = collections.Counter(a["type"] for a in act); usd = collections.defaultdict(float)
for a in act: usd[a["type"]] += float(a.get("usdc_size") or 0)
print(" activity last %g days:" % DAYS, {k: (c[k], round(usd[k])) for k in c})
json.dump({"w": w, "maker": mk, "taker": tak}, open(f"prof_{pref}.json", "w"))
