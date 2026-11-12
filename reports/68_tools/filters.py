"""Which filters known in advance cut the cost of fills? Replay of sim_mk.py: the filter is measured on the first half of
the week (or is static), the result is the second half. d = 2c, the pessimistic fill rule, minimum size.
usage: filters.py SIM.pkl   (needs gross.pkl in the working directory)"""
import os, re, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.rewards.config import RewardsCfg  # noqa: E402
o = pd.read_pickle(sys.argv[1]); g = pd.read_pickle("gross.pkl").set_index("cid")
def half(d, h):
    k = o[(o.d == d) & (o.touch == 1) & (o.half == h)].set_index("cid")
    return k.assign(rpd=k.rew / k.days, lpd=k.loss / k.days, fpd=k.fills / k.days, tpd=k.ntr / k.days)
a, b = half(2.0, 0), half(2.0, 1)
idx = a.index.intersection(b.index); a, b = a.loc[idx], b.loc[idx]
DEADLINE = re.compile(RewardsCfg().deadline_regex, re.I)          # the bot's own filter
dl = g.q.reindex(idx).map(lambda q: bool(DEADLINE.search(q or ""))).values
def show(name, mask):
    k = b[mask]
    if not len(k): print(f"  {name:58s} n=0"); return
    print(f"  {name:58s} n={len(k):4d} reward/d {k.rpd.sum():7.1f} loss/d {k.lpd.sum():7.1f} net/d {(k.rpd - k.lpd).sum():7.1f} kept {(k.rpd - k.lpd).sum() / max(k.rpd.sum(), 1e-9):5.2f} loss per market-day {k.lpd.mean():5.2f} fills/d {k.fpd.mean():4.2f}")
show("all markets", (a.rpd >= 0).values)
print("deadline questions (\"... by November 30?\", \"... before 2027?\", \"released by ...\"):", int(dl.sum()), "of", len(dl))
show("deadline question", dl); show("not a deadline question", ~dl)
print("by 24 h volume ($, snapshot):")
for lo, hi in ((0, 1e-9), (1e-9, 100), (100, 500), (500, 2000), (2000, 1e12)):
    show(f"volume {lo:g}-{hi:g}", ((a.vol24 >= lo) & (a.vol24 < hi)).values)
print("by fills per day in the first half:")
for lo, hi in ((0, .5), (.5, 2), (2, 5), (5, 99)):
    show(f"fills/day {lo:g}-{hi:g}", ((a.fpd >= lo) & (a.fpd < hi)).values)
low = (a.comp < 1).values
print("markets nobody or almost nobody scores in (competitiveness < 1):")
show("all", low); show("not a deadline question", low & ~dl); show("deadline question", low & dl)
print("contested (competitiveness >= 1):")
show("all", ~low); show("not deadline, first-half fills < 0.5 a day, reward >= $1/day", ~low & ~dl & (a.fpd < .5).values & (a.rpd >= 1).values)
