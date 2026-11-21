"""Were the paper interceptor's prices real? For each paper entry (settles.csv) fetch the window's public taker trades and
count the trades that took liquidity for our side at our price or better during the decision zone [T+10, T+238]:
taker BUY of our outcome at <= px, or taker SELL of the other outcome at >= 1 - px (a mint match against the same order)."""
import csv, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
SETTLES, SINCE, UNIT, OUT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
RATE = 0.07
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return None
def paid(cost, sh):
    c = cost / sh; px = c
    for _ in range(20): px = c - RATE * px * (1 - px)
    return px
ents = []
for r in csv.DictReader(open(SETTLES)):
    if r["unit"] == UNIT and r["time_utc"] >= SINCE:
        up, dn = float(r["up"]), float(r["down"])
        side = "Up" if up > 0 else "Down"
        sh = up + dn
        ents.append(dict(key=r["key"], side=side, sh=sh, px=paid(float(r["cost"]), sh), won=r["winner"] == side, pnl=float(r["pnl"])))
have = json.load(open(OUT)) if os.path.exists(OUT) else {}
def one(e):
    a, start = e["key"].rsplit("-", 1); start = int(start)
    ev = get(f"https://gamma-api.polymarket.com/events?slug={a}-updown-5m-{start}")
    if not ev: return e["key"], None
    cid = ev[0]["markets"][0]["conditionId"]
    rows, cur = [], None
    for _ in range(8):
        u = f"https://data-api.polymarket.com/v2/trades?condition={cid}&limit=500&taker_only=true" + (f"&cursor={cur}" if cur else "")
        j = get(u) or {}; d = j.get("data") or []
        rows += [[int(x["timestamp"]), x["side"], x["outcome"], float(x["size"]), float(x["price"])] for x in d]
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or int(d[-1]["timestamp"]) < start: break
    return e["key"], rows
todo = [e for e in ents if e["key"] not in have]
with ThreadPoolExecutor(8) as ex:
    for k, rows in ex.map(one, todo):
        if rows is not None: have[k] = rows
json.dump(have, open(OUT, "w"))
n = hit = 0; shares_at = []; rows_out = []
for e in ents:
    rows = have.get(e["key"])
    if rows is None: continue
    start = int(e["key"].rsplit("-", 1)[1]); px = e["px"] + 0.005
    other = "Down" if e["side"] == "Up" else "Up"
    m = [t for t in rows if start + 10 <= t[0] <= start + 238 and
         ((t[1] == "BUY" and t[2] == e["side"] and t[4] <= px) or (t[1] == "SELL" and t[2] == other and t[4] >= 1 - px))]
    q = sum(t[3] for t in m)
    n += 1; hit += bool(m); shares_at.append(q)
    rows_out.append((e, len(m), q))
print(f"{UNIT} since {SINCE}: entries checked {n}; with public trades at our price or better in the zone: {hit} ({100*hit/max(n,1):.0f} %)")
shares_at.sort()
if shares_at: print(f"  shares others took at our price or better per window: median {shares_at[len(shares_at)//2]:.0f}, quartiles {shares_at[len(shares_at)//4]:.0f} / {shares_at[3*len(shares_at)//4]:.0f}")
for lab, sel in (("with such trades", [x for x in rows_out if x[1]]), ("without", [x for x in rows_out if not x[1]])):
    if sel:
        w = sum(1 for e, _, _ in sel if e["won"]); ex_ = sum(e["px"] for e, _, _ in sel)
        print(f"  {lab:17s} n {len(sel):3d} won {w:3d} vs exp {ex_:5.1f} pnl {sum(e['pnl'] for e, _, _ in sel):+7.2f} avg px {ex_/len(sel):.3f}")
