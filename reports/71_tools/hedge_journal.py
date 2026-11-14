"""Insurance (reports/64) in the journals: every window with an entry since it went live, what the hedge did."""
import json, glob, sys, datetime as dt
T0 = dt.datetime(2026, 11, 11, 20, 31, tzinfo=dt.timezone.utc).timestamp()
out = []
for bot in sys.argv[1:]:
    W = {}
    for f in sorted(glob.glob(f"data/{bot}/2026111*/events.jsonl")):
        for l in open(f):
            if '"status"' in l[:60] or '"prewarm"' in l[:60] or '"strike"' in l[:60]: continue
            try: e = json.loads(l)
            except Exception: continue
            if e["t"] < T0 or "key" not in e: continue
            w = W.setdefault(e["key"], {"entry": None, "hedge": None, "miss": [], "wait": [], "settle": None})
            t = e["type"]
            if t == "lock_entry" and not e.get("hedge") and not e.get("maker"): w["entry"] = e
            elif t == "lock_hedge": w["hedge"] = e
            elif t == "lock_hedge_miss": w["miss"].append(e)
            elif t == "lock_state" and e.get("why") == "hedge_wait": w["wait"].append(e)
            elif t == "settle": w["settle"] = e
    for k, w in W.items():
        e, s = w["entry"], w["settle"]
        if not e or not s: continue
        won = (s["winner"] == "Up") == (e["side"] == "U")
        h = w["hedge"]
        base = (e["shares"] if won else 0) - e["shares"] * e["price"]          # the entry alone, fee aside
        out.append(dict(bot=bot.replace("journal-lock-", ""), key=k, t=e["t"], p=e["p"], px=e["price"], sh=e["shares"], won=won, pnl=s["pnl"],
                        base=round(base, 3), hedged=bool(h), h_px=h["price"] if h else None, h_sh=h["shares"] if h else None,
                        h_p=h["p"] if h else None, h_ask=h.get("ask") if h else None, h_rel=h["rel"] if h else None,
                        n_miss=len(w["miss"]), miss=[(m["rel"], m["ask"], m["cap"], round(m["p"], 3)) for m in w["miss"]],
                        wait=[(x["rel"], x.get("ask"), x.get("cap"), round(x["p"], 3)) for x in w["wait"]][:4]))
json.dump(out, sys.stdout)
