"""Longer record of the same Up/Down windows on Limitless and Polymarket (books every ~3 s, both requests sent together, receive
times kept), 5 min / 15 min for BTC and ETH and the hourly mirrors. Public data only, no account, no orders.
usage: nice python reports/79_tools/record_gap.py HOURS OUT.jsonl"""
import json, sys, threading, time
import requests
S = requests.Session(); S.headers["User-Agent"] = "research-client"
MIN = 20.0
def get(u, t=5):
    try:
        r = S.get(u, timeout=t)
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None
def px_for(levels, need=MIN):
    got = cost = 0.0
    for p, s in levels:
        q = min(need - got, s); got += q; cost += q * p
        if got >= need - 1e-9: return round(cost / got, 4)
    return None
tok, hourly, hourly_t = {}, {}, 0.0
def poly_token(slug):
    if slug not in tok:
        e = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        try: tok[slug] = json.loads(e[0]["markets"][0]["clobTokenIds"])[0]
        except Exception: return None  # noqa: BLE001  (not cached: asked again on the next cycle)
        if len(tok) > 400: tok.pop(next(iter(tok)))
    return tok[slug]
def refresh_hourly():
    global hourly, hourly_t
    found = {}
    for page in (1, 2):
        d = get(f"https://api.limitless.exchange/markets/active?limit=25&page={page}", t=10) or {}
        for m in d.get("data", []):
            md = m.get("metadata") or {}
            if md.get("externalProvider") == "polymarket" and "hourly" in m["slug"] and m["slug"].split("-")[0] in ("btc", "eth", "solana", "xrp"):
                found[m["slug"]] = (md.get("externalSlug"), int(m.get("expirationTimestamp", 0)) // 1000)
    if found: hourly = found
    hourly_t = time.time()
def one(name, ls, ps, end, out, lock):
    t = poly_token(ps)
    if not t: return
    res = {}
    def a(): res["l"] = get(f"https://api.limitless.exchange/markets/{ls}/orderbook"); res["lt"] = time.time()
    def b(): res["p"] = get(f"https://clob.polymarket.com/book?token_id={t}"); res["pt"] = time.time()
    t0 = time.time(); th = [threading.Thread(target=a), threading.Thread(target=b)]; [x.start() for x in th]; [x.join() for x in th]
    l, p = res.get("l"), res.get("p")
    if not l or not p or "bids" not in l or "bids" not in p: return
    lb = sorted(((x["price"], x["size"] / 1e6) for x in l["bids"]), reverse=True); la = sorted((x["price"], x["size"] / 1e6) for x in l["asks"])
    pb = sorted(((float(x["price"]), float(x["size"])) for x in p["bids"]), reverse=True); pa = sorted((float(x["price"]), float(x["size"])) for x in p["asks"])
    r = dict(t=round(t0, 2), k=name, left=round(end - t0, 1), l_ms=int((res["lt"] - t0) * 1000), p_ms=int((res["pt"] - t0) * 1000),
             l_bid=px_for(lb), l_ask=px_for(la), p_bid=px_for(pb), p_ask=px_for(pa), l_bids=lb[:3], l_asks=la[:3], p_bid1=pb[:1], p_ask1=pa[:1])
    with lock:
        out.write(json.dumps(r) + "\n"); out.flush()
stop = time.time() + float(sys.argv[1]) * 3600; lock = threading.Lock()
with open(sys.argv[2], "a") as out:
    while time.time() < stop:
        now = time.time()
        if now - hourly_t > 600: refresh_hourly()
        jobs = []
        for asset in ("btc", "eth"):
            for dur, step in ((5, 300), (15, 900)):
                T = int(now // step * step)
                jobs.append((f"{asset}-{dur}-{T}", f"{asset}-up-or-down-{dur}-min-{T}", f"{asset}-updown-{dur}m-{T}", T + step))
        for ls, (ps, exp) in hourly.items():
            if ps and exp > now: jobs.append((f"{ls.split('-')[0]}-60-{exp}", ls, ps, exp))
        th = [threading.Thread(target=one, args=(*j, out, lock)) for j in jobs]; [x.start() for x in th]; [x.join(8) for x in th]
        time.sleep(max(0.0, 3.0 - (time.time() - now)))
