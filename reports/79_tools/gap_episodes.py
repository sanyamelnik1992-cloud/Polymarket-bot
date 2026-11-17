"""Episodes where the same Up/Down window is cheaper on one venue than the other pays, from record_gap.py output.
Per sample: walk the three recorded Limitless levels against the top Polymarket level, both taker fees taken off.
usage: python reports/79_tools/gap_episodes.py gap.jsonl"""
import json, os, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.fees import fee_per_share as pf
lf = lambda p: 0.05 * (1 - p) * p            # Limitless taker fee per share
rows = [json.loads(l) for l in open(sys.argv[1])]
rows = [r for r in rows if r["p_bid1"] and r["p_ask1"]]
hours = (rows[-1]["t"] - rows[0]["t"]) / 3600
def lock(r):
    """(shares, usd, best net per share, direction) of the fee-covered pair at this sample."""
    best = (0.0, 0.0, -1.0, "")
    pb, pbs = r["p_bid1"][0]; pa, pas = r["p_ask1"][0]
    for d, levels, other, osz in (("L-up", r["l_asks"], 1 - pb, pbs), ("L-down", [(1 - p, s) for p, s in r["l_bids"]], pa, pas)):
        sh = usd = 0.0; top = -1.0
        for p, s in levels:                      # p = price paid on Limitless for the side bought there
            n = 1 - p - other - lf(p) - pf(other)
            if n <= 0: break
            q = min(s, osz - sh)
            if q <= 0: break
            sh += q; usd += q * n; top = max(top, n)
        if usd > best[1]: best = (sh, usd, top, d)
    return best
kind = lambda k: k.split("-")[1]
by = collections.defaultdict(list)
for r in rows: by[r["k"]].append(r)
print(f"hours {hours:.1f}, samples {len(rows)}, windows {len(by)}")
eps = []
for k, v in by.items():
    v.sort(key=lambda r: r["t"]); cur = None
    for r in v:
        sh, usd, top, d = lock(r)
        if usd > 0 and cur and r["t"] - cur["t1"] < 8 and cur["d"] == d:
            cur["t1"] = r["t"]; cur["n"] += 1; cur["usd"] = max(cur["usd"], usd); cur["usd2"] = cur["usd2"] or usd; cur["top"] = max(cur["top"], top)
        elif usd > 0:
            cur = dict(k=k, t0=r["t"], t1=r["t"], n=1, usd=usd, usd1=usd, usd2=0.0, top=top, d=d, left=r["left"], sh=sh); eps.append(cur)
        else: cur = None
for name in ("5", "15", "60"):
    e = [x for x in eps if kind(x["k"]) == name]; w = [k for k in by if kind(k) == name]; n = sum(len(by[k]) for k in w)
    if not w: continue
    long = [x for x in e if x["n"] >= 2]
    print(f"\n{name:>2} min: windows {len(w)}, samples {n} | episodes {len(e)} ({len(e)/hours:.1f}/h), in the last 60 s: {sum(x['left'] <= 60 for x in e)}"
          f" | seen in >= 2 samples in a row (>= 3 s): {len(long)} ({len(long)/hours:.1f}/h)")
    print(f"    $ if taken at the first sample: {sum(x['usd1'] for x in e):.2f} ({sum(x['usd1'] for x in e)/hours*24:.1f}/day) | $ still there one sample later: {sum(x['usd2'] for x in e):.2f} ({sum(x['usd2'] for x in e)/hours*24:.1f}/day)")
    for lo, hi in ((0, 0.25), (0.25, 1), (1, 5), (5, 1e9)):
        g = [x for x in e if lo <= x["usd1"] < hi]
        print(f"    first-sample $ {lo}-{hi if hi < 1e9 else 'up'}: {len(g)} episodes, later-sample $ {sum(x['usd2'] for x in g):.2f}")
    for x in sorted(e, key=lambda x: -x["usd1"])[:8]:
        print(f"    {x['k']:22} left {x['left']:6.0f}s {x['d']:6} {x['sh']:6.0f} sh top {x['top']*100:4.1f}c first ${x['usd1']:.2f} next ${x['usd2']:.2f} samples {x['n']} ({x['t1']-x['t0']:.0f}s)")
