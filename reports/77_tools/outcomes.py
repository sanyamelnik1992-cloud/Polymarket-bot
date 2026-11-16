"""Winners (Up / Down) of every Up/Down window in a trades file, from Gamma.
usage: python outcomes.py trades_crypto.json outcomes.json"""
import json, subprocess, sys, re, time
from concurrent.futures import ThreadPoolExecutor
tr = json.load(open(sys.argv[1])); out = sys.argv[2]
try: have = json.load(open(out))
except Exception: have = {}
slugs = sorted({r["s"] for rows in tr.values() for r in rows if re.search(r"-updown-(5m|15m|4h)-\d{10}$", r["s"])})
now = time.time()
slugs = [s for s in slugs if s not in have and int(s.rsplit("-", 1)[1]) + 1000 < now]
def get(batch):
    q = "&".join(f"slug={s}" for s in batch)
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", f"https://gamma-api.polymarket.com/markets?{q}&closed=true&limit=100"], capture_output=True, text=True).stdout
        try:
            return {m["slug"]: json.loads(m.get("outcomePrices") or "[]") for m in json.loads(r)}
        except Exception:
            time.sleep(1)
    return {}
batches = [slugs[i:i + 40] for i in range(0, len(slugs), 40)]
with ThreadPoolExecutor(8) as ex:
    for d in ex.map(get, batches):
        for s, p in d.items():
            if p and p[0] in ("1", "0") : have[s] = "Up" if p[0] == "1" else "Down"
json.dump(have, open(out, "w"))
print("slugs needed", len(slugs), "known now", len(have))
