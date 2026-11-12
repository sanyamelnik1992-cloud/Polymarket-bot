"""Fetch YES-token books for the N largest active reward pools. usage: books.py ACTIVE.json N OUT.json"""
import json, sys, time
import requests
act = json.load(open(sys.argv[1])); N = int(sys.argv[2])
act.sort(key=lambda m: -m["_rate"]); act = act[:N]
S = requests.Session(); out = {}
toks = [m["tokens"][0]["token_id"] for m in act if len(m.get("tokens") or []) == 2]
for i in range(0, len(toks), 100):
    chunk = toks[i:i + 100]
    for k in range(5):
        try:
            r = S.post("https://clob.polymarket.com/books", json=[{"token_id": t} for t in chunk], timeout=60)
            if r.status_code == 200:
                for b in r.json(): out[b["asset_id"]] = b
                break
            print("status", r.status_code, r.text[:120], flush=True)
        except Exception as e:
            print("err", e, flush=True)
        time.sleep(1 + 2 * k)
    time.sleep(0.15)
json.dump({"ts": time.time(), "books": out}, open(sys.argv[3], "w"))
print("books", len(out), "of", len(toks))
