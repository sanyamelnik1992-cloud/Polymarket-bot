"""Entries of the lock journals (unique windows, first bot listed wins) by how long the model had already been at the entry tier,
by the Binance boost p - p_cl, by the last reason before the entry. usage (server): STEP=300 python reports/76_tools/fresh.py journal-lock-live journal-lock-tier ..."""
import json, glob, sys, os, collections
STEP = int(os.environ.get("STEP", 300)); FEE = 0.006
rows = collections.defaultdict(list); ent = {}; setl = {}; K = {}
for bot in sys.argv[1:]:
    for f in sorted(glob.glob(f"data/{bot}/202611*/events.jsonl")):
        for l in open(f):
            h = l[:60]
            if '"status"' in h or '"prewarm"' in h: continue
            try: e = json.loads(l)
            except Exception: continue
            t = e.get("type")
            if t == "strike": K[e["key"]] = e["K"]
            elif t == "settle": setl[(bot, e["key"])] = e
            elif t == "lock_state": rows[(bot, e["key"])].append(e)
            elif t == "lock_entry" and not e.get("hedge") and not e.get("maker"): ent.setdefault((bot, e["key"]), e)
def winner(key):
    a, T = key.split("-"); k2 = f"{a}-{int(T)+STEP}"
    if key in K and k2 in K: return "Up" if K[k2] >= K[key] else "Down"
E = {}   # unique windows: first bot listed wins
for (bot, key), e in ent.items():
    if key in E: continue
    w = (setl.get((bot, key)) or {}).get("winner") or winner(key)
    if not w: continue
    prev = [r for r in rows[(bot, key)] if r["t"] < e["t"] and r.get("side") == e["side"]]
    last = prev[-1] if prev else None
    # how long has p been at/above the entry's tier?  walk back over rows with p >= tier floor
    tier = 0.99 if e["p"] >= 0.99 else 0.97 if e["p"] >= 0.97 else 0.95 if e["p"] >= 0.95 else 0.90
    held = 0.0
    for r in reversed(rows[(bot, key)]):
        if r["t"] >= e["t"]: continue
        if r.get("side") == e["side"] and (r.get("p") or 0) >= tier: held = e["t"] - r["t"]
        else: break
    dip2 = any((r.get("p") is not None and (r["p"] < tier - 0.03 or r.get("side") != e["side"])) for r in rows[(bot, key)] if e["t"] - 3.0 <= r["t"] < e["t"])
    E[key] = dict(won=(e["side"] == "U") == (w == "Up"), p=e["p"], pcl=e.get("p_cl", e["p"]), price=e["price"], tier=tier, held=held, dip=dip2,
                  lastwhy=last["why"] if last else "none", rel=e["rel"], ask_sz=e.get("ask_sz"), mv1=e.get("mv1"), shock=(e.get("shock_bp") or 0) / max(e.get("shock_lim") or 1e9, 1e-9))
print("unique entry windows", len(E), "lost", sum(not x["won"] for x in E.values()))
def tab(name, f, sel=lambda x: True):
    g = collections.defaultdict(list)
    for x in E.values():
        if sel(x): g[f(x)].append(x)
    print("--", name)
    for k in sorted(g, key=str):
        v = g[k]; n = len(v); l = sum(not x["won"] for x in v); ps = sum((1.0 if x["won"] else 0.0) - x["price"] - FEE for x in v)
        print(f"   {k!s:28} n={n:4} lost={l:3} ({100*l/n:4.1f}%)  per-share sum {ps:+.2f}  ({100*ps/n:+.1f}c/entry)")
tab("tier", lambda x: x["tier"])
tab("held (s the model was already at the tier)", lambda x: "0 first touch" if x["held"] == 0 else "<1s" if x["held"] < 1 else "1-3s" if x["held"] < 3 else ">=3s")
tab("held, p>=.97 only", lambda x: "0 first touch" if x["held"] == 0 else "<1s" if x["held"] < 1 else "1-3s" if x["held"] < 3 else ">=3s", lambda x: x["p"] >= .97)
tab("held, p<.97 only", lambda x: "0 first touch" if x["held"] == 0 else "<1s" if x["held"] < 1 else "1-3s" if x["held"] < 3 else ">=3s", lambda x: x["p"] < .97)
tab("dipped >3c below tier / other side within 3 s before", lambda x: x["dip"])
tab("dipped, p>=.97", lambda x: x["dip"], lambda x: x["p"] >= .97)
tab("Binance boost p - p_cl", lambda x: "boost>=.03" if x["p"] - x["pcl"] >= .03 else "boost .01-.03" if x["p"] - x["pcl"] >= .01 else "boost<.01 / cl higher")
tab("Binance boost, p>=.97", lambda x: "boost>=.03" if x["p"] - x["pcl"] >= .03 else "boost .01-.03" if x["p"] - x["pcl"] >= .01 else "boost<.01 / cl higher", lambda x: x["p"] >= .97)
tab("p_cl itself", lambda x: "pcl>=.99" if x["pcl"] >= .99 else "pcl .97-.99" if x["pcl"] >= .97 else "pcl .95-.97" if x["pcl"] >= .95 else "pcl<.95")
tab("shock ratio (3 s Binance move / pause limit)", lambda x: "<0.3" if x["shock"] < .3 else "<0.6" if x["shock"] < .6 else ">=0.6")
tab("last why before entry", lambda x: x["lastwhy"])
tab("asset", lambda x: [k for k, v in E.items() if v is x][0].split("-")[0])
