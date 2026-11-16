"""Real entries of lock bots by position size and model tier: count, lost, dollars, per-share sum.
usage (server, repo root): python reports/76_tools/by_size.py journal-lock-live journal-lock-tier ..."""
import json, glob, sys, time, collections
for d in sys.argv[1:]:
    ent = {}; g = collections.defaultdict(lambda: [0, 0, 0.0, 0.0]); hed = 0.0
    for f in sorted(glob.glob(f"data/{d}/*/events.jsonl")):
        for line in open(f):
            if '"lock_entry"' not in line and '"settle"' not in line: continue
            e = json.loads(line)
            if e["type"] == "lock_entry" and not e.get("hedge"): ent[e["key"]] = e
            elif e["type"] == "settle" and e["key"] in ent:
                en = ent.pop(e["key"]); big = "big(>=8sh)" if en["shares"] >= 8 else "mid(4-8sh)" if en["shares"] >= 4 else "small(<4sh)"
                tier = "p>=.99" if en["p"] >= .99 else "p.97-.99" if en["p"] >= .97 else "p.95-.97" if en["p"] >= .95 else "p<.95"
                for k in (("size", big), ("tier", tier), ("size+tier", big + " " + tier)):
                    x = g[k]; x[0] += 1; x[1] += e["pnl"] < 0; x[2] += e["pnl"]; x[3] += e["pnl"] / en["shares"]
    print("==", d)
    for k in sorted(g): print(f"  {k[0]:10} {k[1]:24} n={g[k][0]:3} lost={g[k][1]:2} pnl=${g[k][2]:+7.2f} per-share-sum={g[k][3]:+.2f}")
