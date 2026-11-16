"""Real entries of a lock bot by model probability, window second, price, distance and day: count, lost, per-share sum.
usage (server, repo root): python reports/75_tools/entries_by_tier.py journal-lock-15-live journal-lock-15-tier ..."""
import json, glob, sys, time, collections
def load(d):
    ent = {}; out = []
    for f in sorted(glob.glob(f"data/{d}/*/*")):
        for line in open(f):
            try: e = json.loads(line)
            except Exception: continue
            ty = e.get("type")
            if ty == "lock_entry": ent[e["key"]] = e
            if ty == "settle" and e.get("key") in ent:
                en = ent.pop(e["key"]); sh = en.get("shares") or 1
                out.append(dict(t=en["t"], key=e["key"], pnl=e["pnl"], per=e["pnl"] / sh, rel=en.get("rel", 0), p=en.get("p", 0), price=en.get("price", 0), dist=abs(en.get("dist_bp", 0)), sig=en.get("sigma_bp", 0), spot=en.get("spot_bp"), calm=en.get("calm", True), sh=sh))
    return out
def tab(name, rows, f):
    g = collections.defaultdict(list)
    for r in rows: g[f(r)].append(r)
    print("--", name)
    for k in sorted(g):
        v = g[k]; n = len(v); l = sum(r["pnl"] < 0 for r in v)
        print(f"  {k!s:18} n={n:3} lost={l:2} ({100*l/n:4.1f}%) per_share={sum(r['per'] for r in v):+.2f} avg_price={sum(r['price'] for r in v)/n:.3f}")
for d in sys.argv[1:]:
    rows = load(d); print("=====", d, len(rows), "lost", sum(r["pnl"] < 0 for r in rows), "pnl", round(sum(r["pnl"] for r in rows), 2), "per-share sum", round(sum(r["per"] for r in rows), 2))
    tab("p", rows, lambda r: "p<.93" if r["p"] < .93 else "p<.95" if r["p"] < .95 else "p<.97" if r["p"] < .97 else "p>=.97")
    tab("rel", rows, lambda r: "rel<250" if r["rel"] < 250 else "rel<260" if r["rel"] < 260 else "rel<275" if r["rel"] < 275 else "rel>=275")
    tab("price", rows, lambda r: "px<=.90" if r["price"] <= .905 else "px<=.93" if r["price"] <= .935 else "px>.93")
    tab("dist/sigma z", rows, lambda r: (lambda z: "z<3" if z < 3 else "z<5" if z < 5 else "z<8" if z < 8 else "z>=8")(r["dist"] / max(r["sig"], 1e-9)))
    tab("dist bp", rows, lambda r: "d<2" if r["dist"] < 2 else "d<4" if r["dist"] < 4 else "d<6" if r["dist"] < 6 else "d>=6")
    tab("day", rows, lambda r: time.strftime("%m-%d", time.gmtime(r["t"])))
    for r in rows:
        if r["pnl"] < 0: print("  LOSS", time.strftime("%d %H:%M", time.gmtime(r["t"])), r["key"], f"rel={r['rel']:.0f} p={r['p']:.3f} px={r['price']} dist={r['dist']:.2f} sig={r['sig']:.2f} spot={r['spot']} calm={r['calm']} sh={r['sh']:.0f}")
