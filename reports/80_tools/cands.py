"""Candidate wallets from the cached all-takers sample (r16/mkt_taker.json): late buys at >= 0.95, $30-200 a window."""
import json, collections, sys
d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "mkt_taker.json"))   # reports/77_tools/mkt_map.py output
agg = collections.defaultdict(lambda: dict(win=set(), usd=0.0))
for slug, v in d.items():
    dur = 300 if "-5m-" in slug else 900
    end = int(slug.rsplit("-", 1)[1]) + dur
    for r in v["rows"]:
        if r["side"] != "BUY" or r["px"] < 0.95: continue
        left = end - r["t"]
        if left > 0.3 * dur or left < -5: continue
        a = agg[r["w"]]; a["win"].add(slug); a["usd"] += r["sz"] * r["px"]
c = [w for w, a in agg.items() if len(a["win"]) >= 4 and 30 <= a["usd"] / len(a["win"]) <= 250]
json.dump(c, open("cands.json", "w")); print(len(c))
