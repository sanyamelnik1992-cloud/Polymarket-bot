"""Recent trades of wallets (public data): market kinds, taker share, entry prices, timing vs market end."""
import json, subprocess, sys, collections, time, re
from concurrent.futures import ThreadPoolExecutor
def get(url):
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", url], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1)
    return {}
def fetch(w, taker_only, since):
    rows, cur = [], None
    for _ in range(8):
        u = f"https://data-api.polymarket.com/v2/trades?user={w}&limit=500&taker_only={'true' if taker_only else 'false'}&start={since}"
        if cur: u += f"&cursor={cur}"
        j = get(u); d = j.get("data") or []; rows += d
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur: break
    return rows
def one(w, since):
    a = fetch(w, False, since); t = fetch(w, True, since)
    return w, a, t
if __name__ == "__main__":
    ws = json.load(open(sys.argv[1])); since = int(time.time() - float(sys.argv[2]) * 86400); out = sys.argv[3]
    try: have = json.load(open(out))
    except Exception: have = {}
    todo = [w for w in ws if w not in have]
    with ThreadPoolExecutor(6) as ex:
        for w, a, t in ex.map(lambda w: one(w, since), todo):
            tk = {(x["transaction_hash"], x["token_id"], x["size"]) for x in t}
            have[w] = [dict(s=x["slug"], o=x["outcome"], side=x["side"], sz=float(x["size"]), px=float(x["price"]), t=int(x["timestamp"]),
                            tk=(x["transaction_hash"], x["token_id"], x["size"]) in tk, title=x.get("title", "")[:90], ev=x.get("event_slug", "")) for x in a]
    json.dump(have, open(out, "w"))
    print("wallets", len(have), "trades", sum(len(v) for v in have.values()))
