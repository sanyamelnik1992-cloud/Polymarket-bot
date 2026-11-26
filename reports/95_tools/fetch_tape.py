"""Public tape of the crypto Up/Down windows of a date range -> windows.json (Gamma outcomes) and trades.json (all taker trades).

  python fetch_tape.py FROM TO OUTDIR [assets=btc] [durations=5m]       FROM / TO = YYYY-MM-DD (UTC, TO exclusive)

windows.json : [{slug, asset, kind, start, end, win, cid, outs}]
trades.json  : slug -> [[ts, side B/S, outcome, size, price, wallet]]  (data-api v2, taker rows only: one row per trade)
Public data only; ~2 minutes per 4 days of BTC 5m with 16 threads. Resumable (trades.json keeps what it has)."""
import calendar, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

FROM, TO, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
ASSETS = (sys.argv[4] if len(sys.argv) > 4 else "btc").split(",")
KINDS = (sys.argv[5] if len(sys.argv) > 5 else "5m").split(",")
DUR = {"5m": 300, "15m": 900}
os.makedirs(OUT, exist_ok=True)


def day(s):
    y, m, d = map(int, s.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0))


def get(u, tries=5):
    for k in range(tries):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try:
            return json.loads(r)
        except Exception:
            time.sleep(1 + k)
    return {}


def outcomes():
    t_from, t_to = day(FROM), day(TO)
    W = [(f"{a}-updown-{k}-{st}", a, k, st, st + DUR[k]) for a in ASSETS for k in KINDS for st in range(t_from, t_to, DUR[k])]

    def batch(ws):
        q = "&".join(f"slug={w[0]}" for w in ws)
        res = {}
        for m in get(f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100") or []:
            op = json.loads(m.get("outcomePrices") or "[]")
            outs = json.loads(m.get("outcomes") or "[]")
            if "1" in op:
                res[m["slug"]] = dict(win=outs[op.index("1")], cid=m["conditionId"], outs=outs)
        return res

    O = {}
    with ThreadPoolExecutor(8) as ex:
        for r in ex.map(batch, [W[i:i + 40] for i in range(0, len(W), 40)]):
            O.update(r)
    out = [dict(slug=s, asset=a, kind=k, start=st, end=en, **O[s]) for s, a, k, st, en in W if s in O]
    json.dump(out, open(f"{OUT}/windows.json", "w"))
    print("windows", len(W), "with outcome", len(out), flush=True)
    return out


def one(w):
    rows, cur = [], None
    for _ in range(60):
        u = f"https://data-api.polymarket.com/v2/trades?condition={w['cid']}&limit=500&taker_only=true" + (f"&cursor={cur}" if cur else "")
        j = get(u)
        d = j.get("data") or []
        rows += [[int(x["timestamp"]), x["side"][0], x["outcome"], float(x["size"]), float(x["price"]), x["proxy_wallet"]] for x in d]
        pg = j.get("pagination") or {}
        cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur or int(d[-1]["timestamp"]) < w["start"]:
            break
    return w["slug"], [r for r in rows if w["start"] - 5 <= r[0] <= w["end"] + 5]


if __name__ == "__main__":
    W = outcomes()
    path = f"{OUT}/trades.json"
    have = json.load(open(path)) if os.path.exists(path) else {}
    todo = [w for w in W if w["slug"] not in have]
    print("todo", len(todo), flush=True)
    done = 0
    with ThreadPoolExecutor(16) as ex:
        for s, rows in ex.map(one, todo):
            have[s] = rows
            done += 1
            if done % 300 == 0:
                print("done", done, time.strftime("%H:%M:%S"), flush=True)
                json.dump(have, open(path, "w"))
    json.dump(have, open(path, "w"))
    print("windows", len(have), "trades", sum(len(v) for v in have.values()))
