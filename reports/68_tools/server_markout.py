"""Where the price went after the fills of the server's rewards bot, and what its exits cost against simply holding.
Needs OUT_DIR/fh.json from server_hist.py. Downloads the public rewards list (to resolve the slugs the report cuts at 55
characters) and 5-minute price histories of the markets with fills.   python reports/68_tools/server_markout.py OUT_DIR
Prices are Polymarket's displayed ones: in a wide book they jump, so single rows mean little, sums do."""
import base64, bisect, calendar, collections, json, math, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests
out = sys.argv[1] if len(sys.argv) > 1 else "."
d = json.load(open(f"{out}/fh.json")); F, H = d["fills"], d["hedges"]
CH = 1440 + 9 * 60 + 36                                  # rules of 13 Nov went live at 09:36 UTC (minutes from 12 Nov 00:00)
T0 = calendar.timegm((2026, 11, 12, 0, 0, 0))
def tm(x): return (int(x["d"][3:]) - 12) * 1440 + int(x["t"][:2]) * 60 + int(x["t"][3:])
S = requests.Session()
def page(off):
    p = {"order_by": "rate_per_day", "position": "DESC", "page_size": 100}
    if off: p["next_cursor"] = base64.b64encode(str(off).encode()).decode()
    for k in range(4):
        try:
            r = S.get("https://clob.polymarket.com/rewards/markets/multi", params=p, timeout=40)
            if r.status_code == 200: return r.json().get("data") or []
        except Exception:
            pass
        time.sleep(1 + k)
    return []
def gamma(slug):
    for closed in ("false", "true"):
        try:
            r = S.get("https://gamma-api.polymarket.com/markets", params={"slug": slug, "closed": closed}, timeout=30).json()
            if r: return r[0]
        except Exception:
            pass
def hist(tok):
    for k in range(3):
        try:
            r = S.get("https://clob.polymarket.com/prices-history", params={"market": tok, "startTs": T0, "endTs": int(time.time()), "fidelity": 5}, timeout=40)
            if r.status_code == 200: return tok, [(h["t"], h["p"]) for h in r.json().get("history", [])]
        except Exception:
            pass
        time.sleep(1)
    return tok, []
by = collections.defaultdict(list)
with ThreadPoolExecutor(8) as ex:
    for rows in ex.map(page, range(0, 16000, 100)):
        for m in rows: by[m["market_slug"][:55]].append(m["tokens"][0]["token_id"])
cands = {}
for s in sorted({f["slug"] for f in F}):
    if s in by: cands[s] = sorted(set(by[s]))
    elif len(s) < 55:                                   # no longer in the rewards list: ask Gamma by the exact slug
        g = gamma(s)
        if g: cands[s] = [json.loads(g.get("clobTokenIds") or "[]")[0]]
with ThreadPoolExecutor(6) as ex:
    PX = dict(ex.map(hist, [t for v in cands.values() for t in v]))
def at(h, t):
    i = bisect.bisect_left(h, (t, -1)); return h[min(i, len(h) - 1)][1]
def pick(slug, t, y):
    """history of the market: the report cuts slugs, so of several candidates take the one priced nearest to our fill"""
    best = None
    for tok in cands.get(slug, []):
        h = PX.get(tok) or []
        if not h: continue
        i = bisect.bisect_left(h, (t, -1)); p0 = h[max(i - 1, 0)][1]
        if best is None or abs(p0 - y) < best[0]: best = (abs(p0 - y), h)
    return best[1] if best else None
# ---- episodes: from flat to flat (or still open), with how they were closed
ev = sorted([(tm(f), 0, "f", f) for f in F] + [(tm(h), 1, "h", h) for h in H], key=lambda x: (x[0], x[1]))
pos, eps = {}, []
for t, _, k, x in ev:
    p = pos.get(x["slug"])
    if k == "f":
        s = "Y" if x["side"] == "YES" else "N"
        if p is None:
            p = pos[x["slug"]] = dict(slug=x["slug"], t0=t, alone=x["alone"], side=s, Y=0.0, N=0.0, cY=0.0, cN=0.0, pnl=0.0, exits=0, taker=0,
                                      px=x["price"], sh=0.0, tend=None)
        elif s != p["side"]: p["exits"] += 1
        p[s] += x["size"]; p["c" + s] += x["size"] * x["price"]
        if s == p["side"]: p["sh"] += x["size"]
    else:
        if p is None: continue                           # the hedge of a fill the reports did not list
        s = "Y" if x["buy"] == "YES" else "N"
        p[s] += x["size"]; p["c" + s] += x["size"] * (x["cost"] + 1 - x["held"]); p["taker"] += 1
    m = min(p["Y"], p["N"])
    if m > 1e-9:
        cy, cn = p["cY"] * m / p["Y"], p["cN"] * m / p["N"]
        p["pnl"] += m - cy - cn; p["Y"] -= m; p["N"] -= m; p["cY"] -= cy; p["cN"] -= cn
    if abs(p["Y"] - p["N"]) < 1:
        p["tend"] = t; eps.append(p); del pos[x["slug"]]
eps += list(pos.values())
rows = []
for p in eps:
    if p["sh"] < 5: continue
    y = p["px"] if p["side"] == "Y" else 1 - p["px"]; t = T0 + p["t0"] * 60
    h = pick(p["slug"], t, y)
    if not h: continue
    sg = 1 if p["side"] == "Y" else -1
    p["m10"], p["m60"], p["m360"] = (sg * (at(h, t + dt) - y) for dt in (600, 3600, 21600))
    p["hold6"] = p["sh"] * sg * (at(h, t + 21600) - y); p["hold"] = p["sh"] * sg * (h[-1][1] - y)
    p["how"] = "open" if p["tend"] is None else "own bid" if p["exits"] and not p["taker"] else "taker" if p["taker"] and not p["exits"] else "mixed"
    rows.append(p)
print(f"positions with a price history: {len(rows)} of {sum(p['sh'] >= 5 for p in eps)}")
def mo(name, sel):
    n = sum(p["sh"] for p in sel)
    if n: print(f"  {name:28s} n {len(sel):3d}  +10 min {100 * sum(p['m10'] * p['sh'] for p in sel) / n:+5.1f}c  +1 h {100 * sum(p['m60'] * p['sh'] for p in sel) / n:+5.1f}c"
                f"  +6 h {100 * sum(p['m360'] * p['sh'] for p in sel) / n:+5.1f}c  now {100 * sum(p['hold'] for p in sel) / n:+5.1f}c a share")
print("price after the opening fill, our side (minus = against us):")
mo("all", rows); mo("before 13 Nov 09:36", [p for p in rows if p["t0"] < CH]); mo("after", [p for p in rows if p["t0"] >= CH])
mo("alone in the zone", [p for p in rows if p["alone"]]); mo("others in the zone", [p for p in rows if not p["alone"]])
print("how the positions were closed, and the same positions simply held:")
for k in ("taker", "own bid", "mixed", "open"):
    v = [p for p in rows if p["how"] == k]; n = sum(p["sh"] for p in v)
    if n: print(f"  {k:8s} n {len(v):3d} shares {n:5.0f} realised {sum(p['pnl'] for p in v):+8.2f} ({100 * sum(p['pnl'] for p in v) / n:+5.1f}c) | held: 6 h later {sum(p['hold6'] for p in v):+8.2f}, now {sum(p['hold'] for p in v):+8.2f} ({100 * sum(p['hold'] for p in v) / n:+5.1f}c)")
ex = [p for p in rows if p["how"] == "own bid"]
print("exits by our own bid, by the time since the fill:")
for lo, hi in ((0, 30), (30, 180), (180, 10**6)):
    v = [p for p in ex if lo <= p["tend"] - p["t0"] < hi]; n = sum(p["sh"] for p in v)
    if n: print(f"  {f'{lo}-{hi} min' if hi < 10**6 else f'after {lo} min':14s}: n {len(v):2d} realised {100 * sum(p['pnl'] for p in v) / n:+5.1f}c  held instead {100 * sum(p['hold'] for p in v) / n:+5.1f}c a share")
df = [p["pnl"] - p["hold"] for p in ex]
if len(df) > 2:
    print(f"  exit minus hold, per position: mean {statistics.mean(df):+.2f}$, t = {statistics.mean(df) / (statistics.stdev(df) / math.sqrt(len(df))):.1f}, the exit was worse in {sum(x < 0 for x in df)} of {len(df)}")
for N in (1, 3):
    tot = sum(p["pnl"] if (-p["pnl"] / p["sh"] <= 0.06 + 1e-9 or p["tend"] - p["t0"] >= N * 60) else p["hold"] for p in ex)
    print(f"  with the pair capped at $1.06 for the first {N} h: {tot:+.2f} (as it was: {sum(p['pnl'] for p in ex):+.2f})")
