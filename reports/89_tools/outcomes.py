"""Gamma outcomes of window slugs -> outcomes.json {slug: "Up"|"Down"} (closed windows only; cached)."""
import json, subprocess, sys, os, time
from concurrent.futures import ThreadPoolExecutor
def get(u):
    for k in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "30", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1 + k)
    return []
out = json.load(open("outcomes.json")) if os.path.exists("outcomes.json") else {}
slugs = [s for s in json.load(open(sys.argv[1])) if s not in out]
def one(s):
    e = get(f"https://gamma-api.polymarket.com/events?slug={s}")
    if not e: return s, None
    m = e[0]["markets"][0]
    try:
        pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        names = json.loads(m.get("outcomes") or "[]")
    except Exception:
        return s, None
    if not m.get("closed") or len(pr) != 2 or max(pr) < 0.99: return s, None
    return s, names[pr.index(max(pr))]
with ThreadPoolExecutor(16) as ex:
    for s, o in ex.map(one, slugs):
        if o: out[s] = o
json.dump(out, open("outcomes.json", "w"))
print("outcomes", len(out))
