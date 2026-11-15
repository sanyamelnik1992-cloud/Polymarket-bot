"""15m lock bots: every entry with features and outcome, plus per-window reasons that kept the bot out."""
import json, glob, sys, collections
out = {"entries": [], "windows": {}}
for bot in sys.argv[1:]:
    states, ent, setl, miss = {}, {}, {}, collections.Counter()
    whys = {}
    for f in sorted(glob.glob(f"data/{bot}/202611*/events.jsonl")):
        for l in open(f):
            h = l[:70]
            if '"status"' in h or '"prewarm"' in h or '"strike"' in h: continue
            try: e = json.loads(l)
            except Exception: continue
            t = e["type"]
            if t == "lock_state":
                whys.setdefault(e["key"], []).append((e["why"], e.get("rel"), e.get("p"), e.get("ask"), e.get("side"), e.get("dist_bp")))
            elif t == "lock_entry" and not e.get("hedge") and not e.get("maker"): ent[e["key"]] = e
            elif t == "lock_miss": miss[e["key"]] += 1
            elif t == "settle": setl[e["key"]] = e
    b = bot.replace("journal-lock-", "")
    for k, e in ent.items():
        s = setl.get(k)
        if not s: continue
        won = (s["winner"] == "Up") == (e["side"] == "U")
        sgn = 1 if e["side"] == "U" else -1
        out["entries"].append(dict(bot=b, key=k, t=e["t"], rel=e["rel"], side=e["side"], p=e["p"], price=e["price"], shares=e["shares"],
                                   ask=e.get("ask"), limit=e.get("limit"), won=won, pnl=s["pnl"], dist=e["dist_bp"], sigma=e["sigma_bp"],
                                   adv3=-sgn * e["mv3"], misses=miss.get(k, 0)))
    # windows with a qualifying state but no entry: why
    W = {}
    for k, ws in whys.items():
        if k in ent: continue
        kinds = collections.Counter(w[0] for w in ws)
        sig_like = [w for w in ws if w[0].startswith("risk:") or w[0] in ("ask_too_high", "no_ask", "ask_too_low", "edge", "tier", "tier_early")]
        if sig_like or miss.get(k):
            best = max(sig_like, key=lambda w: w[2] or 0) if sig_like else None
            W[k] = dict(kinds=dict(kinds), misses=miss.get(k, 0), best=best)
    out["windows"][b] = W
json.dump(out, sys.stdout)
