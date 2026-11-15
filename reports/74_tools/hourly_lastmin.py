"""Feasibility of a last-minute entry on the HOURLY Up/Down markets (reports/74 section 4, idea 3) from public data only:
Gamma (closed hourly events and their winners), the Data API (trades of the closing two minutes) and Binance 1 s klines.
For every trade of the closing minute: how far Binance was from the hour's open, in sigmas of the time left (z), which side
that favours, at what price the favoured side traded, and whether it won.

usage: python reports/74_tools/hourly_lastmin.py OUT.jsonl [DAYS]      then it prints the tables
"""
import json, math, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests

SERIES = {"btc": ("btc-up-or-down-hourly", "BTCUSDT"), "eth": ("eth-up-or-down-hourly", "ETHUSDT")}
out, days = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
S = requests.Session()
S.headers["User-Agent"] = "research"


def get(url, params=None):
    for k in range(5):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 + 2 * k)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5 + k)
    return None


def iso(s):
    return int(time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)


def events(asset):
    res, off = [], 0
    t_min = time.time() - days * 86400
    while True:
        evs = get("https://gamma-api.polymarket.com/events", {"series_slug": SERIES[asset][0], "closed": "true", "limit": 100,
                                                              "offset": off, "order": "startDate", "ascending": "false"}) or []
        for e in evs:
            m = e["markets"][0]
            st = iso(m.get("eventStartTime") or e.get("startDate"))
            p = json.loads(m.get("outcomePrices") or "[0,0]"); oc = json.loads(m.get("outcomes") or "[]")
            if not oc or max(map(float, p)) < 0.99:
                continue
            res.append({"asset": asset, "start": st, "cond": m["conditionId"], "winner": oc[[float(x) for x in p].index(max(map(float, p)))]})
        if not evs or min(r["start"] for r in res or [{"start": 0}]) < t_min or off > 2000:
            break
        off += 100
    return [r for r in res if r["start"] >= t_min]


def one(ev):
    end = ev["start"] + 3600
    rows, cursor = [], None
    for _ in range(40):
        q = {"condition": ev["cond"], "limit": 500, "taker_only": "true"}
        if cursor:
            q["cursor"] = cursor
        d = get("https://data-api.polymarket.com/v2/trades", q)
        if not d or "data" not in d:
            break
        rows += d["data"]
        pg = d.get("pagination") or {}
        cursor = pg.get("next_cursor")
        if not pg.get("has_more") or not cursor or (d["data"] and d["data"][-1]["timestamp"] < end - 130):
            break
    tr = [[x["timestamp"], x["side"], x["outcome"], float(x["size"]), float(x["price"])] for x in rows if end - 120 <= x["timestamp"] <= end + 5]
    sym = SERIES[ev["asset"]][1]
    k1 = get("https://api.binance.com/api/v3/klines", {"symbol": sym, "interval": "1h", "startTime": ev["start"] * 1000, "limit": 1})
    ks = get("https://api.binance.com/api/v3/klines", {"symbol": sym, "interval": "1s", "startTime": (end - 720) * 1000, "limit": 725})
    if not k1 or not ks:
        return None
    return {**ev, "open": float(k1[0][1]), "close": float(k1[0][4]), "px": [[k[0] // 1000, float(k[4])] for k in ks], "trades": sorted(tr)}


def collect():
    have = set()
    try:
        for line in open(out):
            r = json.loads(line); have.add((r["asset"], r["start"]))
    except FileNotFoundError:
        pass
    evs = [e for a in SERIES for e in events(a) if (e["asset"], e["start"]) not in have]
    print("events to fetch", len(evs), flush=True)
    with ThreadPoolExecutor(4) as ex, open(out, "a") as f:
        for r in ex.map(one, evs):
            if r:
                f.write(json.dumps(r) + "\n")


def analyse():
    R = [json.loads(l) for l in open(out)]
    print("hourly windows", len(R), {a: sum(r["asset"] == a for r in R) for a in SERIES})
    chk = sum((r["close"] >= r["open"]) == (r["winner"] == "Up") for r in R)
    print(f"winner = Binance 1h close >= open in {chk} of {len(R)}")
    rows = []
    for r in R:
        px = dict(r["px"]); end = r["start"] + 3600
        secs = sorted(px)
        rets = [math.log(px[b] / px[a]) for a, b in zip(secs, secs[1:]) if b <= end - 120]
        sig = (sum(x * x for x in rets) / max(len(rets), 1)) ** 0.5 or 1e-6
        for ts, side, outcome, size, price in r["trades"]:
            left = end - ts
            if not 1 <= left <= 60 or side != "BUY":
                continue
            s = px.get(ts - 1) or px.get(ts)
            if not s:
                continue
            d = math.log(s / r["open"])
            fav = "Up" if d >= 0 else "Down"
            z = abs(d) / (sig * math.sqrt(left))
            rows.append(dict(asset=r["asset"], start=r["start"], left=left, z=z, dist_bp=abs(d) * 1e4, fav=fav, outcome=outcome, size=size,
                             price=price, won=(outcome == r["winner"]), sig_bp=sig * 1e4))
    import collections
    print("taker BUY trades in the closing minute:", len(rows))
    fav = [x for x in rows if x["outcome"] == x["fav"]]
    def tab(name, xs):
        if not xs: print(f"  {name:44s} -"); return
        w = collections.defaultdict(list)
        for x in xs: w[(x["asset"], x["start"])].append(x)
        sh = sum(x["size"] for x in xs); pnl = sum(x["size"] * ((1 if x["won"] else 0) - x["price"]) for x in xs)
        lostw = sum(1 for v in w.values() if not v[0]["won"])
        print(f"  {name:44s} windows {len(w):3d} lost {lostw:2d} | trades {len(xs):4d} shares {sh:8.0f} avg px {sum(x['size']*x['price'] for x in xs)/sh:.3f} "
              f"pnl/share {100*pnl/sh:+5.1f}c | median shares/window {sorted(sum(y['size'] for y in v) for v in w.values())[len(w)//2]:.0f}")
    print("== buys of the side Binance favours, by z (distance / sigma of the time left) and price")
    for zlo, zhi in ((0, 1), (1, 2), (2, 3), (3, 5), (5, 99)):
        tab(f"z {zlo}-{zhi}, any price", [x for x in fav if zlo <= x["z"] < zhi])
    for zlo in (2, 3, 5):
        for plo, phi in ((0.5, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 1.0)):
            tab(f"z >= {zlo}, price {plo}-{phi}", [x for x in fav if x["z"] >= zlo and plo <= x["price"] < phi])
    for lo, hi in ((30, 60), (10, 30), (1, 10)):
        tab(f"z >= 3, price < 0.98, {lo}-{hi} s left", [x for x in fav if x["z"] >= 3 and x["price"] < 0.98 and lo <= x["left"] <= hi])


if __name__ == "__main__":
    collect()
    analyse()
