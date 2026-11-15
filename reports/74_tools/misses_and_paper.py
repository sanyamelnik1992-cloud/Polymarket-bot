import json, glob, sys, collections
out = {"miss": [], "maker": {}}
for bot in ("journal-lock-15-live", "journal-lock-live"):
    ent, misses, states, setl = {}, collections.defaultdict(list), collections.defaultdict(list), {}
    for f in sorted(glob.glob(f"data/{bot}/202611*/events.jsonl")):
        for l in open(f):
            h = l[:70]
            if '"status"' in h or '"prewarm"' in h or '"strike"' in h: continue
            try: e = json.loads(l)
            except Exception: continue
            t = e["type"]
            if t == "lock_entry" and not e.get("hedge"): ent[e["key"]] = e
            elif t == "lock_miss": misses[e["key"]].append(e)
            elif t == "lock_state": states[e["key"]].append(e)
            elif t == "settle": setl[e["key"]] = e
    for k, ms in misses.items():
        m0 = ms[0]
        after = [(round(s["rel"], 1), s["why"], s.get("ask"), round(s.get("p") or 0, 3)) for s in states[k] if s["t"] > m0["t"]][:6]
        e = ent.get(k)
        out["miss"].append(dict(bot=bot[13:], key=k, t=m0["t"], rel=m0["rel"], side=m0["side"], p=m0["p"], ask=m0.get("ask"), limit=m0.get("limit"),
                                ask_sz=m0.get("ask_sz"), ms=m0.get("ms"), n=len(ms), entered=bool(e), entry_px=e["price"] if e else None,
                                entry_rel=e["rel"] if e else None, pnl=setl[k]["pnl"] if k in setl else None,
                                winner=setl[k]["winner"] if k in setl else None, after=after))
for bot in ("journal-lock-maker3", "journal-lock-tier-wide", "journal-lock-99"):
    seen = {}
    for f in sorted(glob.glob(f"data/{bot}/202611*/events.jsonl")):
        for l in open(f):
            if '"settle"' in l[:60]:
                e = json.loads(l); seen[e["key"]] = e
    by = collections.OrderedDict()
    import datetime as dt
    for k, e in sorted(seen.items(), key=lambda kv: kv[1]["t"]):
        d = dt.datetime.utcfromtimestamp(e["t"]).strftime("%d"); x = by.setdefault(d, [0, 0, 0.0]); x[0] += 1; x[1] += e["pnl"] < 0; x[2] += e["pnl"]
    out["maker"][bot] = {k: [v[0], v[1], round(v[2], 2)] for k, v in by.items()}
json.dump(out, sys.stdout)
