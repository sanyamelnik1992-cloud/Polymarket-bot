"""Tier variants on the journal states: per window the first non-guard row whose p reaches a tier and whose seen ask is within
that tier, bought at the seen ask + 0.6 c fee (optimistic: no FAK misses). usage: [STEP=300] python reports/75_tools/tier_replay.py JOURNAL ..."""
import json, glob, sys, os, collections
STEP = int(os.environ.get("STEP", 900)); FEE = 0.006
K = {}; W = {}; S = collections.defaultdict(list)
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
            elif t in ("lock_state", "lock_entry", "lock_miss") and e.get("p") is not None and e.get("side") and e.get("ask") is not None:
                if e.get("why") in ("binance_against", "binance_jump", "shock", "agree"): continue   # guards blocked: not an entry
                S[e["key"]].append((e["rel"], e["side"], e["p"], e["ask"], e["t"]))
def winner(key):
    a, T = key.split("-"); k2 = f"{a}-{int(T)+STEP}"
    if key in K and k2 in K: return "Up" if K[k2] >= K[key] else "Down"
    return W.get(key)
def rule(tiers, rel_lo=240, name=""):
    by = collections.defaultdict(lambda: [0, 0, 0.0])
    for k, rows in S.items():
        w = winner(k)
        if not w: continue
        for rel, side, p, ask, t in sorted(rows):
            if rel < rel_lo: continue
            tier = next(((pm, am) for pm, am in tiers if p >= pm), None)
            if tier and ask <= tier[1] + 1e-9 and ask >= 0.5:
                won = (side == "U") == (w == "Up")
                b = by[tier[0]]; b[0] += 1; b[1] += not won; b[2] += (1.0 if won else 0.0) - ask - FEE
                break
    tot = [sum(b[i] for b in by.values()) for i in range(3)]
    print(f"{name:34} ALL n={tot[0]:4d} lost={tot[1]:3d} sum/share={tot[2]:+.2f} | " + "  ".join(f"p>={pm}: {b[0]}/{b[1]} {b[2]:+.2f}" for pm, b in sorted(by.items(), reverse=True)))
T4 = [(0.99, 0.98), (0.97, 0.95), (0.95, 0.93), (0.90, 0.91)]
rule(T4, name="now: bottom 0.90")
rule(T4[:3] + [(0.93, 0.91)], name="bottom 0.93")
rule(T4[:3], name="no bottom tier (p>=.95)")
rule(T4[:2], name="p>=.97 only")
rule(T4[:1], name="p>=.99 only")
rule(T4, 260, name="now, from sec 260")
rule(T4[:3] + [(0.93, 0.91)], 260, name="bottom 0.93, from sec 260")
