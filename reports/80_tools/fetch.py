"""All trades of candidate wallets since a time (public data-api v2), cached per wallet."""
import json, subprocess, sys, time, os
from concurrent.futures import ThreadPoolExecutor
def get(url):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "60", url], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return {}
def fetch(w, since, pages):
    rows, cur = [], None
    for _ in range(pages):
        u = f"https://data-api.polymarket.com/v2/trades?user={w}&limit=500&taker_only=false&start={since}"
        if cur: u += f"&cursor={cur}"
        j = get(u); dd = j.get("data") or []; rows += dd
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not dd or not pg.get("has_more") or not cur: break
    return w, rows
if __name__ == "__main__":
    ws = json.load(open(sys.argv[1])); since = int(sys.argv[2]); pages = int(sys.argv[3]); out = sys.argv[4]
    have = json.load(open(out)) if os.path.exists(out) else {}
    todo = [w for w in ws if w not in have]
    with ThreadPoolExecutor(6) as ex:
        for w, rows in ex.map(lambda w: fetch(w, since, pages), todo):
            have[w] = [dict(s=x.get("slug", ""), o=x.get("outcome"), side=x["side"], sz=float(x["size"]), px=float(x["price"]),
                            t=int(x["timestamp"]), h=x.get("transaction_hash")) for x in rows]
    json.dump(have, open(out, "w"))
    print("wallets", len(have), "trades", sum(len(v) for v in have.values()))
