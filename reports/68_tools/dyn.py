"""How long an empty pool stays empty: snapshots of the rewards collection (snap.py) every 10 minutes.
For a minimum-size quote placed at the first snapshot and held: the reward it would earn with the pool and the
competitiveness changing under it, against the static estimate of the first snapshot.
usage: dyn.py SNAP_DIR CANDS.pkl"""
import glob, json, sys
import numpy as np, pandas as pd
files = sorted(glob.glob(sys.argv[1] + "/s_*.json"))
snaps = [json.load(open(f)) for f in files]
k = pd.read_pickle(sys.argv[2]).set_index("cid")
t0 = snaps[0]["ts"]
print("snapshots:", len(snaps), " span %.0f min" % ((snaps[-1]["ts"] - t0) / 60), " markets in the first:", len(snaps[0]["rows"]))
first = snaps[0]["rows"]
def q_of(r, d): return r["ms"] * max((r["v"] - d) / r["v"], 0) ** 2 if r["v"] > 0 else 0.0
def share(r, d=2.0):
    q = q_of(r, d); return q / (r["comp"] * r["rate"] + q) if q > 0 else 0.0
# candidate universe: the long-lived filters of cands.pkl (price range, > 2 days, not live) and a pool >= $20
cids = [c for c, r in first.items() if c in k.index and r["rate"] >= 20 and r["ms"] <= 60]
empty = [c for c in cids if first[c]["comp"] * first[c]["rate"] < 1.0]
print("candidates with a pool >= $20 and min size <= 60: %d; empty (under one point of total score) in the first snapshot: %d, pools $%.0f/day" % (len(cids), len(empty), sum(first[c]["rate"] for c in empty)))
print("\nEmpty at the start; minutes later -> still listed / still empty / pool at least half of the start / our share at 2c (median, mean)")
for s in snaps[1:]:
    dt = (s["ts"] - t0) / 60; rows = s["rows"]
    here = [c for c in empty if c in rows]
    still = [c for c in here if rows[c]["comp"] * rows[c]["rate"] < 1.0]
    big = [c for c in here if rows[c]["rate"] >= 0.5 * first[c]["rate"]]
    sh = np.array([share(rows[c]) if c in rows else 0.0 for c in empty])
    pool_now = sum(rows[c]["rate"] for c in here)
    print(f"  +{dt:4.0f} min: listed {len(here):3d}  empty {len(still):3d} ({100*len(still)/len(empty):3.0f}%)  pool kept {len(big):3d} ({100*len(big)/len(empty):3.0f}%)  "
          f"share median {np.median(sh):.2f} mean {sh.mean():.2f}  pools now ${pool_now:.0f}")
# reward of holding a min-size quote in the 45 best markets of the first snapshot (by static estimate per $), re-selected never
def reward_path(sel, d=2.0):
    tot_static = sum(first[c]["rate"] * share(first[c], d) for c in sel)
    acc = 0.0
    for a, b in zip(snaps[:-1], snaps[1:]):
        dt = (b["ts"] - a["ts"]) / 86400
        for c in sel:
            r = a["rows"].get(c)
            if r: acc += r["rate"] * share(r, d) * dt
    span = (snaps[-1]["ts"] - t0) / 86400
    return tot_static, acc / span
rank = sorted(cids, key=lambda c: -(first[c]["rate"] * share(first[c]) / first[c]["ms"]))
for n in (20, 45, 100):
    sel = rank[:n]
    st, dyn = reward_path(sel)
    print(f"\nhold the best {n} markets of the first snapshot for the whole span: static estimate ${st:.0f}/day, with pools and competition changing ${dyn:.0f}/day ({100*dyn/st:.0f}%)")
# re-select every snapshot (what the bot does every 10 minutes): best N by the current numbers
for n in (20, 45, 100):
    acc = 0.0; turnover = []; prev = None
    for a, b in zip(snaps[:-1], snaps[1:]):
        rows = a["rows"]; dt = (b["ts"] - a["ts"]) / 86400
        cc = [c for c, r in rows.items() if c in k.index and r["rate"] >= 5 and r["ms"] <= 60 and r["rate"] * share(r) >= 1.0]
        sel = sorted(cc, key=lambda c: -(rows[c]["rate"] * share(rows[c]) / rows[c]["ms"]))[:n]
        acc += sum(rows[c]["rate"] * share(rows[c]) for c in sel) * dt
        if prev is not None: turnover.append(len(set(sel) - set(prev)) / max(len(sel), 1))
        prev = sel
    span = (snaps[-1]["ts"] - t0) / 86400
    print(f"re-select the best {n} every snapshot: ${acc/span:.0f}/day by the numbers of each moment; markets replaced per 10 min: {100*np.mean(turnover):.0f}%")
# how pools move: distribution of rate changes over the span for markets empty at the start
last = snaps[-1]["rows"]
ch = [(first[c]["rate"], last[c]["rate"] if c in last else 0.0, last[c]["comp"] * last[c]["rate"] if c in last else 0.0) for c in empty]
cut = sum(1 for a, b, q in ch if b < 0.5 * a); up = sum(1 for a, b, q in ch if b > 1.5 * a)
got = [q for a, b, q in ch if q >= 1.0]
print(f"\nafter the whole span, of {len(empty)} pools empty at the start: pool cut by more than half {cut} ({100*cut/len(empty):.0f}%), raised by more than half {up}; "
      f"got competition {len(got)} ({100*len(got)/len(empty):.0f}%), total score that came: median {np.median(got) if got else 0:.0f} points (our min-size quote at 2c scores 6)")
