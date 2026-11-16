"""How many windows a day offer the p >= 0.99 side at <= 0.98 / <= 0.99, how many the first journal (the live bot) entered, and why not.
usage (server): STEP=300 python reports/76_tools/opp99.py journal-lock-live journal-lock-tier journal-lock-tier-pb ..."""
import json, glob, sys, os, collections
STEP = int(os.environ.get("STEP", 300)); LIVE = sys.argv[1]
K = {}; W = {}; opp = {}; live_rows = collections.defaultdict(list); live_ent = {}; live_miss = collections.Counter(); t0 = 1e18; t1 = 0
for bot in sys.argv[1:]:
    for f in sorted(glob.glob(f"data/{bot}/202611*/events.jsonl")):
        for l in open(f):
            h = l[:60]
            if '"status"' in h or '"prewarm"' in h: continue
            try: e = json.loads(l)
            except Exception: continue
            t = e.get("type")
            if t == "strike": K[e["key"]] = e["K"]
            elif t == "settle": W[e["key"]] = e["winner"]
            elif t in ("lock_state", "lock_entry", "lock_miss") and e.get("p") is not None and e.get("side"):
                if bot == LIVE:
                    t0 = min(t0, e["t"]); t1 = max(t1, e["t"])
                    if t == "lock_entry": live_ent[e["key"]] = e
                    elif t == "lock_miss": live_miss[e["key"]] += 1
                    else: live_rows[e["key"]].append(e)
                if e["p"] >= 0.99 and e.get("ask") is not None and e.get("why") not in ("binance_against", "binance_jump", "shock", "agree"):
                    o = opp.setdefault(e["key"], dict(ask=9, sz=0, side=e["side"], t=e["t"]))
                    if e["ask"] < o["ask"]: o.update(ask=e["ask"], sz=e.get("ask_sz") or 0, side=e["side"])
def winner(key):
    a, T = key.split("-"); k2 = f"{a}-{int(T)+STEP}"
    if key in K and k2 in K: return "Up" if K[k2] >= K[key] else "Down"
    return W.get(key)
days = (t1 - t0) / 86400
print(f"live journal span {days:.1f} days")
for cap in (0.98, 0.99):
    ks = [k for k, o in opp.items() if o["ask"] <= cap + 1e-9 and t0 <= o["t"] <= t1 and winner(k)]
    lost = sum((opp[k]["side"] == "U") != (winner(k) == "Up") for k in ks)
    took = [k for k in ks if k in live_ent]
    notk = [k for k in ks if k not in live_ent]
    why = collections.Counter()
    for k in notk:
        ws = [r["why"] for r in live_rows.get(k, []) if r["p"] >= 0.99]
        w = "missed (FAK)" if live_miss.get(k) else ("risk: " + next(x for x in ws if x.startswith("risk"))[:40] if any(x.startswith("risk") for x in ws) else (collections.Counter(ws).most_common(1)[0][0] if ws else "live never saw p>=.99"))
        why[w] += 1
    slots = collections.Counter(int(k.split("-")[1]) for k in ks)
    sz = sorted(opp[k]["sz"] for k in ks)
    print(f"p>=0.99 with ask <= {cap}: windows {len(ks)} ({len(ks)/days:.1f}/day), lost {lost}; live entered {len(took)} ({len(took)/days:.1f}/day); "
          f"median size at best ask {sz[len(sz)//2] if sz else 0:.0f}, quartile {sz[len(sz)//4] if sz else 0:.0f}; time slots with 2+ coins: {sum(1 for v in slots.values() if v > 1)}")
    print("   why live did not enter:", dict(why.most_common(8)))
