"""Do makers visit empty reward pools for a minute or two? Poll the books of pools nobody scores in every ~20 s.
A two-sided quote that can score exists when the size-adjusted spread (first price with min_size cumulative on each side)
is under twice the max spread. usage: poll.py SNAP.json CANDS.pkl ACTIVE.json OUT.jsonl MINUTES [N=200]"""
import json, sys, time
import requests, pandas as pd
snap = json.load(open(sys.argv[1]))["rows"]; k = pd.read_pickle(sys.argv[2]).set_index("cid")
act = {m["condition_id"]: m for m in json.load(open(sys.argv[3]))}
MIN = float(sys.argv[5]); N = int(sys.argv[6]) if len(sys.argv) > 6 else 200
cids = [c for c, r in snap.items() if c in k.index and c in act and r["rate"] >= 20 and r["ms"] <= 60 and r["comp"] * r["rate"] < 1.0]
cids = sorted(cids, key=lambda c: -snap[c]["rate"])[:N]
tok = {act[c]["tokens"][0]["token_id"]: c for c in cids}
S = requests.Session(); t_end = time.time() + MIN * 60
print("markets", len(cids), "pools $%.0f/day" % sum(snap[c]["rate"] for c in cids), flush=True)
def adj(levels, ms):
    cum = 0.0
    for p, s in levels:
        cum += s
        if cum >= ms - 1e-9: return p
    return None
n = 0
with open(sys.argv[4], "a") as f:
    while time.time() < t_end:
        t = time.time(); rec = {}
        toks = list(tok)
        for i in range(0, len(toks), 100):
            try:
                r = S.post("https://clob.polymarket.com/books", json=[{"token_id": x} for x in toks[i:i + 100]], timeout=30)
                if r.status_code != 200: continue
                for b in r.json():
                    c = tok.get(b["asset_id"])
                    if not c: continue
                    ms = snap[c]["ms"]
                    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []), reverse=True)
                    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])
                    pb, pa = adj(bids, ms), adj(asks, ms)
                    rec[c] = [pb, pa, dict(bids).get(pb) if pb is not None else None, dict(asks).get(pa) if pa is not None else None]
            except Exception:
                pass
        f.write(json.dumps({"t": t, "b": rec}) + "\n"); f.flush(); n += 1
        time.sleep(max(0.0, 20 - (time.time() - t)))
print("polls", n)
