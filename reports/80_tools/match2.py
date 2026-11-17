"""Match wallets' daily buys in crypto Up/Down markets to the screenshot table (complete days 11-14 Nov first)."""
import json, collections, sys, time
T = {"11-09": (17, 999.55), "11-10": (32, 2398.13), "11-11": (20, 2322.86), "11-12": (27, 2603.79), "11-13": (43, 4272.38), "11-14": (39, 2861.85), "11-15": (52, 3975.21)}
FULL = ["11-11", "11-12", "11-13", "11-14"]
d = json.load(open(sys.argv[1]))
res = []
for w, rows in d.items():
    if not rows: continue
    t0 = min(r["t"] for r in rows)
    for tz in range(-8, 10):
        for pmin in (0.0, 0.9, 0.95):
            day = collections.defaultdict(lambda: [set(), 0.0])
            for r in rows:
                if r["side"] != "BUY" or "updown" not in r["s"] or r["px"] < pmin: continue
                k = time.strftime("%m-%d", time.gmtime(r["t"] + tz * 3600))
                day[k][0].add(r["s"]); day[k][1] += r["sz"] * r["px"]
            err = sum(abs(day[k][1] - T[k][1]) / T[k][1] if k in day else 1 for k in FULL)
            res.append((err, w, tz, pmin, {k: (len(day[k][0]), round(day[k][1], 2)) for k in sorted(day) if k in T}, t0))
res.sort(key=lambda r: r[0])
seen = set()
for e, w, tz, pmin, dd, t0 in res:
    if w in seen: continue
    seen.add(w)
    print(f"{e:6.3f} {w[:10]} tz{tz:+d} p>={pmin} first {time.strftime('%m-%d %H:%M', time.gmtime(t0))} {dd}")
    if len(seen) >= 8: break
