"""Is the model right? Every window of the lock journals (lock_state / lock_entry / lock_miss rows carry the model p and side), the
first row with p >= 0.90 in each closing-minute bucket, against the outcome (next strike >= this strike, checked against settles).
usage (server, repo root): [STEP=300] python reports/75_tools/state_calib.py journal-lock-15-tier journal-lock-15-live ...   (STEP = window seconds, default 900)"""
import json, glob, sys, time, collections
import os; STEP = int(os.environ.get("STEP", 900)); K = {}; W = {}; S = collections.defaultdict(list); seen = set()
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
                sig = (e["key"], round(e["rel"], 1))
                if sig in seen: continue
                seen.add(sig)
                S[e["key"]].append((e["rel"], e["side"], e["p"], abs(e.get("dist_bp") or 0), e.get("sigma_bp") or 0, e.get("ask"), e.get("why", t), e.get("spot_bp"), e["t"]))
def winner(key, step=STEP):
    a, T = key.split("-"); k2 = f"{a}-{int(T)+step}"
    if key in K and k2 in K: return "Up" if K[k2] >= K[key] else "Down"
    return W.get(key)
bad = sum(1 for k in W if k in K and f"{k.split('-')[0]}-{int(k.split('-')[1])+STEP}" in K and winner(k) != W[k])
print("windows with states", len(S), "strike-vs-settle mismatches", bad, "of", len(W))
RB = [(240, 250), (250, 260), (260, 275), (275, 300)]
PB = [(0.90, 0.93), (0.93, 0.95), (0.95, 0.97), (0.97, 0.99), (0.99, 1.01)]
def run(name, filt=lambda k, r: True, since=0):
    print("==", name)
    for rlo, rhi in RB:
        line = f"  rel {rlo}-{rhi}: "
        for plo, phi in PB:
            n = l = 0
            for k, rows in S.items():
                w = winner(k)
                if not w: continue
                rows = sorted(rows)
                x = [r for r in rows if rlo <= r[0] < rhi and r[2] >= 0.90 and r[8] >= since and filt(k, r)]
                if not x: continue
                r = x[0]
                if not (plo <= r[2] < phi): continue
                n += 1; l += (r[1] == "U") != (w == "Up")
            line += f"p{plo:.2f}: {n:4d}/{l:3d} ({100*l/max(n,1):4.1f}%)  "
        print(line)
run("all 4 coins, all days, first state with p>=.90 in the rel bucket, bucketed by its p  [n/lost]")
for a in ("btc", "eth", "sol", "xrp"): run(a, lambda k, r, a=a: k.startswith(a))
run("only states that had an ask <= 0.95 (tradeable-ish)", lambda k, r: r[5] is not None and r[5] <= 0.95)
import calendar
run("since 15 Nov 00:00", since=calendar.timegm((2026, 11, 15, 0, 0, 0)))
run("before 15 Nov", lambda k, r: r[8] < calendar.timegm((2026, 11, 15, 0, 0, 0)))
