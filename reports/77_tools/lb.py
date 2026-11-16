"""Monthly and weekly leaderboards (top 300) of every category -> lb.json  (data-api /v2/leaderboard, time_period, category)."""
import json, subprocess, collections
def get(url):
    r = subprocess.run(["curl", "-sS", "-m", "60", url], capture_output=True, text=True).stdout
    try: return json.loads(r)
    except Exception: return {}
out = {}
for c in ("overall", "crypto", "sports", "politics", "finance", "economics", "weather", "tech", "culture", "mentions"):
    for tp in ("month", "week"):
        rows, cur = [], None
        while len(rows) < 300:
            u = f"https://data-api.polymarket.com/v2/leaderboard?limit=100&time_period={tp}" + (f"&category={c}" if c != "overall" else "")
            if cur: u += f"&cursor={cur}"
            j = get(u); d = j.get("data") or []
            rows += d
            cur = (j.get("pagination") or {}).get("next_cursor")
            if not d or not cur: break
        out[f"{c}/{tp}"] = rows
        print(c, tp, len(rows), "top pnl", round(rows[0]["pnl"]) if rows else None, "#100", round(rows[99]["pnl"]) if len(rows) > 99 else None, "#300", round(rows[-1]["pnl"]) if rows else None)
json.dump(out, open("lb.json", "w"))
users = collections.Counter(r["user_id"] for v in out.values() for r in v)
print("unique wallets", len(users))
