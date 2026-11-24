"""Crypto leaderboard (week, month, day) -> each wallet's exchange PnL series over the last N days:
economic pnl, position pnl, maker rebates, rewards, fees, volume, trades."""
import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
def get(url):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", url], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return {}
lb = {}
for tp in ("day", "week", "month"):
    rows, cur = [], None
    while len(rows) < 300:
        u = f"https://data-api.polymarket.com/v2/leaderboard?limit=100&time_period={tp}&category=crypto" + (f"&cursor={cur}" if cur else "")
        j = get(u); d = j.get("data") or []
        rows += d; cur = (j.get("pagination") or {}).get("next_cursor")
        if not d or not cur: break
    lb[tp] = rows
    print(tp, len(rows), rows[0] if rows else None, flush=True)
json.dump(lb, open("lb.json", "w"))
users = sorted({r["user_id"] for v in lb.values() for r in v})
print("wallets", len(users), flush=True)
def pnl(u):
    j = get(f"https://data-api.polymarket.com/v2/user-pnl?user={u}&time_period=all&interval=all&fidelity=1d")
    return u, (j.get("data") or {}).get("points") or []
up = {}
with ThreadPoolExecutor(12) as ex:
    for u, pts in ex.map(pnl, users):
        up[u] = pts
json.dump(up, open("upnl.json", "w"))
print("pnl series", sum(1 for v in up.values() if v))
