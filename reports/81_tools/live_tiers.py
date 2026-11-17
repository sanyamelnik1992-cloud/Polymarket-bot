"""Real-money entries of the live lock bots by model probability (run on the server in data/): entries, lost, $ total."""
import json, glob, collections
for j in ("journal-lock-live", "journal-lock-15-live", "journal-lock-99-live"):
    ent, pnl = {}, {}
    for f in sorted(glob.glob(j + "/*/events.jsonl")):
        for line in open(f):
            if '"lock_entry"' in line or '"settle"' in line:
                e = json.loads(line)
                if e["type"] == "lock_entry" and not e.get("hedge"): ent.setdefault(e["key"], e)
                elif e["type"] == "settle": pnl[e["key"]] = e["pnl"]
    b = collections.defaultdict(lambda: [0, 0, 0.0])
    for k, e in ent.items():
        if k not in pnl: continue
        p = e.get("p", 0); g = ">=0.99" if p >= 0.99 else "0.97-0.99" if p >= 0.97 else "0.95-0.97" if p >= 0.95 else "<0.95"
        x = b[g]; x[0] += 1; x[1] += pnl[k] < 0; x[2] += pnl[k]
    print(j, {g: (v[0], v[1], round(v[2], 2)) for g, v in sorted(b.items())})
