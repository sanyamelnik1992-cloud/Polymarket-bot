"""Per-entry features from the lock journals: what the bot saw in the window before it bought."""
import json, glob, sys, datetime as dt
T0 = dt.datetime(2026, 11, 10, 18, 52, tzinfo=dt.timezone.utc).timestamp()
BOTS = sys.argv[1:] or ["journal-lock-live", "journal-lock-tier-pb", "journal-lock-tier"]
BLOCK = ("binance_against", "shock", "binance_jump")
out = []
for bot in BOTS:
    states, ent, setl, hedges = {}, {}, {}, {}
    for f in sorted(glob.glob(f"data/{bot}/2026111*/events.jsonl")):
        for l in open(f):
            if '"type":"status"' in l[:60] or '"prewarm"' in l[:60]:
                continue
            try:
                e = json.loads(l)
            except Exception:
                continue
            if e["t"] < T0:
                continue
            t = e["type"]
            if t == "lock_state":
                states.setdefault(e["key"], []).append(e)
            elif t == "lock_entry":
                (hedges if e.get("hedge") else ent).setdefault(e["key"], []).append(e)
            elif t == "settle":
                setl[e["key"]] = e
    for k, es in ent.items():
        s = setl.get(k)
        if not s or es[0].get("maker"):
            continue
        e = es[0]
        won = (s["winner"] == "Up") == (e["side"] == "U")
        pre = [x for x in states.get(k, []) if x["t"] < e["t"] and x.get("side") == e["side"]]
        blocks = [x for x in pre if x["why"] in BLOCK]
        last_block = e["t"] - max((x["t"] for x in blocks), default=-1e18)
        dmax = max([x["dist_bp"] for x in pre if "dist_bp" in x] + [e["dist_bp"]])
        sgn = 1 if e["side"] == "U" else -1
        out.append(dict(bot=bot.replace("journal-lock-", ""), key=k, t=e["t"], rel=e["rel"], side=e["side"], p=e["p"],
                        price=e["price"], shares=e["shares"], won=won, pnl=s["pnl"], dist=e["dist_bp"],
                        adv1=-sgn * e["mv1"], adv3=-sgn * e["mv3"], shock=e.get("shock_bp"), shock_lim=e.get("shock_lim"),
                        sigma=e["sigma_bp"], since_block=round(last_block, 2) if blocks else None, nblocks=len(blocks),
                        drop=round(dmax - e["dist_bp"], 3), hedged=k in hedges))
json.dump(out, sys.stdout)
