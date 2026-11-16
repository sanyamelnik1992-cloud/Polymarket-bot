"""Cross-venue cost of the same Up/Down window (Limitless + Polymarket) from sample_books.py output, with both taker fees.
usage: python reports/79_tools/cross_cost.py books.jsonl"""
import json, os, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.fees import fee_per_share as pf
lf = lambda p: 0.05 * (1 - p) * p            # Limitless "new venue" taker fee per share
rows = [json.loads(l) for l in open(sys.argv[1])]
rows = [r for r in rows if None not in (r["l_bid"], r["l_ask"], r["p_bid"], r["p_ask"])]
print("samples", len(rows), "windows", len({r["k"] for r in rows}), "span min", round((rows[-1]["t"] - rows[0]["t"]) / 60, 1))
out = []
for r in rows:
    a_cost = r["l_ask"] + (1 - r["p_bid"]); a_fee = lf(r["l_ask"]) + pf(1 - r["p_bid"])      # Up on Limitless + Down on Polymarket
    b_cost = r["p_ask"] + (1 - r["l_bid"]); b_fee = pf(r["p_ask"]) + lf(1 - r["l_bid"])      # Up on Polymarket + Down on Limitless
    g = max(1 - a_cost, 1 - b_cost); n = max(1 - a_cost - a_fee, 1 - b_cost - b_fee)
    dur = int(r["k"].split("-")[1]); left = dur * 60 - r["rel"]
    out.append(dict(r, gross=g, net=n, left=left, lsp=r["l_ask"] - r["l_bid"], psp=r["p_ask"] - r["p_bid"], mid_gap=abs((r["l_ask"] + r["l_bid"]) / 2 - (r["p_ask"] + r["p_bid"]) / 2)))
def q(v, f): v = sorted(v); return v[min(len(v) - 1, int(f * len(v)))]
print(f"Limitless spread (20 shares): median {q([o['lsp'] for o in out], .5):.3f}, Polymarket: {q([o['psp'] for o in out], .5):.3f}; gap between the two mids: median {q([o['mid_gap'] for o in out], .5):.3f}, 90% {q([o['mid_gap'] for o in out], .9):.3f}")
for name, sel in (("all", lambda o: True), ("5m", lambda o: "-5-" in o["k"]), ("15m", lambda o: "-15-" in o["k"]), ("last 60 s of a window", lambda o: o["left"] <= 60), ("before last 60 s", lambda o: o["left"] > 60)):
    v = [o for o in out if sel(o)]
    if not v: continue
    gp = [o for o in v if o["gross"] > 0]; np_ = [o for o in v if o["net"] > 0]; n1 = [o for o in v if o["net"] > 0.01]
    print(f"{name:22} samples {len(v):5} | gross edge > 0: {len(gp):4} ({100*len(gp)/len(v):4.1f}%) | net of fees > 0: {len(np_):4} ({100*len(np_)/len(v):4.1f}%) | net > 1c: {len(n1):4} | best net {max(o['net'] for o in v)*100:+.1f}c | median gross {q([o['gross'] for o in v], .5)*100:+.1f}c")
best = sorted(out, key=lambda o: -o["net"])[:12]
for o in best: print(f"  {o['k']:22} left {o['left']:5.0f}s net {o['net']*100:+5.1f}c gross {o['gross']*100:+5.1f}c | L {o['l_bid']:.3f}/{o['l_ask']:.3f} (sz {o['l_bid_sz']:.0f}/{o['l_ask_sz']:.0f}) P {o['p_bid']:.3f}/{o['p_ask']:.3f}")
# episodes: consecutive samples of a window with net > 0
ep = collections.defaultdict(list)
for o in out:
    if o["net"] > 0: ep[o["k"]].append(o)
print("windows with any net-positive sample:", len(ep), "of", len({o['k'] for o in out}), {k: (len(v), f"{max(x['net'] for x in v)*100:.1f}c", f"{v[-1]['t']-v[0]['t']:.0f}s") for k, v in ep.items()})
