"""Simultaneous books of the same Up/Down window on Limitless and Polymarket. One JSON line per (window, sample)."""
import json, sys, time, threading
from lib import get
MIN = 20.0   # shares needed at a price level to count (walk the book until 20 shares)
def px_for(levels, need=MIN):
    got = cost = 0.0
    for p, s in levels:
        q = min(need - got, s); got += q; cost += q * p
        if got >= need - 1e-9: return cost / got
    return None
tok = {}
def poly_up_token(slug):
    if slug not in tok:
        e = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        tok[slug] = json.loads(e[0]["markets"][0]["clobTokenIds"])[0] if e else None
    return tok[slug]
def one(asset, dur, T, out):
    ls = f"{asset}-up-or-down-{dur}-min-{T}"; ps = f"{asset}-updown-{dur}m-{T}"
    t = poly_up_token(ps)
    if not t: return
    res = {}
    def a(): res["l"] = (time.time(), get(f"https://api.limitless.exchange/markets/{ls}/orderbook", tries=1, t=6))
    def b(): res["p"] = (time.time(), get(f"https://clob.polymarket.com/book?token_id={t}", tries=1, t=6))
    th = [threading.Thread(target=a), threading.Thread(target=b)]; [x.start() for x in th]; [x.join() for x in th]
    l, p = res.get("l", (0, None))[1], res.get("p", (0, None))[1]
    if not l or not p or "bids" not in l or "bids" not in p: return
    lb = sorted(((x["price"], x["size"] / 1e6) for x in l["bids"]), reverse=True); la = sorted((x["price"], x["size"] / 1e6) for x in l["asks"])
    pb = sorted(((float(x["price"]), float(x["size"])) for x in p["bids"]), reverse=True); pa = sorted((float(x["price"]), float(x["size"])) for x in p["asks"])
    r = dict(t=round(time.time(), 2), k=f"{asset}-{dur}-{T}", rel=round(time.time() - T, 1),
             l_bid=px_for(lb), l_ask=px_for(la), p_bid=px_for(pb), p_ask=px_for(pa),
             l_bid1=lb[0] if lb else None, l_ask1=la[0] if la else None, p_bid1=pb[0] if pb else None, p_ask1=pa[0] if pa else None,
             l_bid_sz=sum(s for pr, s in lb if lb and pr >= lb[0][0] - 0.02), l_ask_sz=sum(s for pr, s in la if la and pr <= la[0][0] + 0.02))
    out.write(json.dumps(r) + "\n"); out.flush()
end = time.time() + float(sys.argv[1]) * 60
with open(sys.argv[2], "a") as out:
    while time.time() < end:
        now = time.time(); th = []
        for asset in ("btc", "eth"):
            for dur, step in ((5, 300), (15, 900)):
                T = int(now // step * step)
                th.append(threading.Thread(target=one, args=(asset, dur, T, out)))
        [x.start() for x in th]; [x.join() for x in th]
        time.sleep(max(0.0, 2.0 - (time.time() - now)))
