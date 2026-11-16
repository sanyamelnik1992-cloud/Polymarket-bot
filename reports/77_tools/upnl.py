"""Daily PnL series (exchange's own accounting) of leaderboard wallets: consistency, rebates, rewards."""
import json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
lb = json.load(open("lb.json"))
cats = sys.argv[1].split(",")
users = []
for c in cats:
    for r in lb[c]:
        if r["user_id"] not in users:
            users.append(r["user_id"])
def get(u):
    url = f"https://data-api.polymarket.com/v2/user-pnl?user={u}&time_period=all&interval=all&fidelity=1d"
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", url], capture_output=True, text=True).stdout
        try:
            return u, json.loads(r).get("data", {}).get("points") or []
        except Exception:
            pass
    return u, []
try:
    have = json.load(open("upnl.json"))
except Exception:
    have = {}
todo = [u for u in users if u not in have]
with ThreadPoolExecutor(8) as ex:
    for u, pts in ex.map(get, todo):
        have[u] = pts
json.dump(have, open("upnl.json", "w"))
print("wallets", len(have), "fetched", len(todo), "empty", sum(1 for u in users if not have.get(u)))
