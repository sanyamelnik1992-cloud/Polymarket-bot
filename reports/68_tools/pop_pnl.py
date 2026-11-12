"""Monthly PnL series (public /v2/user-pnl) for maker wallets seen in >= MIN_MARKETS rewarded markets.
usage: pop_pnl.py POP.json OUT.jsonl [MIN_MARKETS=2] [THREADS=4]"""
import json, sys, time, collections, threading
from concurrent.futures import ThreadPoolExecutor
import requests
tl = threading.local()
def get(url, params):
    s = getattr(tl, "s", None)
    if s is None: s = tl.s = requests.Session()
    for k in range(7):
        try:
            r = s.get(url, params=params, timeout=60)
            if r.status_code == 200: return r.json()
            if r.status_code in (429, 503): time.sleep(3 + 4 * k); continue
            return {"_status": r.status_code}
        except Exception:
            pass
        time.sleep(1 + k)
    return None
pop = json.load(open(sys.argv[1]))
MINM = int(sys.argv[3]) if len(sys.argv) > 3 else 2
TH = int(sys.argv[4]) if len(sys.argv) > 4 else 4
nm = collections.Counter(); fills = collections.Counter()
for cid, o in pop["markets"].items():
    for w, n in o["makers"].items(): nm[w] += 1; fills[w] += n
have = set()
try:
    for line in open(sys.argv[2]): have.add(json.loads(line)["w"])
except FileNotFoundError:
    pass
todo = [w for w in nm if nm[w] >= MINM and w not in have]
print("wallets to fetch", len(todo), "already", len(have), flush=True)
KEEP = ("timestamp", "reward_income", "maker_rebate", "taker_rebate", "yield_income", "position_pnl", "realized_pnl", "unrealized_pnl",
        "trade_pnl", "economic_pnl", "volume_usdc", "trade_count", "fees")
def one(w):
    j = get("https://data-api.polymarket.com/v2/user-pnl", {"user": w, "interval": "1m", "fidelity": "1d"})
    pts = ((j or {}).get("data") or {}).get("points") or []
    v = get("https://data-api.polymarket.com/v2/value", {"user": w})
    val = ((v or {}).get("data") or {}).get("value")
    return {"w": w, "markets": nm[w], "fills": fills[w], "value": val, "pts": [[p.get(k) for k in KEEP] for p in pts], "status": (j or {}).get("_status")}
lock = threading.Lock(); n = 0
with open(sys.argv[2], "a") as f, ThreadPoolExecutor(TH) as ex:
    for rec in ex.map(one, todo):
        f.write(json.dumps(rec) + "\n"); n += 1
        if n % 200 == 0: f.flush(); print(n, "done", flush=True)
print("done", n)
