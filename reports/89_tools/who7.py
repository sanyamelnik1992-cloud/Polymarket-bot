"""Top crypto wallets of the last 7 days with >= 300 trades: what they trade (market kind), taker vs maker share, prices,
sizes, merges/redeems. Uses the last ~2000 trades of each wallet (taker_only false and true)."""
import json, subprocess, time, re, collections
from concurrent.futures import ThreadPoolExecutor
def get(url):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", url], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return {}
def paged(base, pages):
    rows, cur = [], None
    for _ in range(pages):
        j = get(base + (f"&cursor={cur}" if cur else ""))
        d = j.get("data") or []
        rows += d
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur: break
    return rows
def kind(slug):
    s = slug or ""
    m = re.match(r"(btc|eth|sol|xrp|doge|bnb|hype)-updown-(5m|15m|4h)-", s)
    if m: return f"updown-{m.group(2)}"
    if re.match(r"(bitcoin|ethereum|solana|xrp|dogecoin|bnb|hyperliquid)-up-or-down-", s): return "updown-1h/day"
    if "updown" in s or "up-or-down" in s: return "updown-other"
    return "other-crypto" if any(k in s for k in ("bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "crypto")) else "non-crypto"
rows = json.load(open("prof7.json"))
cand = [r for r in rows if r["n"] >= 300 and r["pnl"] > 5000]
cand.sort(key=lambda r: -r["pnl"])
cand = cand[:60]
print("candidates", len(cand), flush=True)
def one(r):
    w = r["w"]
    allr = paged(f"https://data-api.polymarket.com/v2/trades?user={w}&limit=500&taker_only=false", 4)
    tk = paged(f"https://data-api.polymarket.com/v2/trades?user={w}&limit=500&taker_only=true", 4)
    act = paged(f"https://data-api.polymarket.com/v2/activity?user={w}&limit=500", 2)
    return w, allr, tk, act
out = {}
with ThreadPoolExecutor(8) as ex:
    for w, allr, tk, act in ex.map(one, cand):
        out[w] = dict(all=allr, taker=tk, act=act)
json.dump(out, open("who7_raw.json", "w"))
print("fetched", len(out))
