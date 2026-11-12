"""What the fills of a paper run of the rewards farmer were: how they came, in which kind of market, what they cost.
usage: run_an.py JOURNAL_RUN_DIR STATE.json"""
import json, os, re, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.rewards.config import RewardsCfg  # noqa: E402
ev = [json.loads(l) for l in open(os.path.join(sys.argv[1], "events.jsonl"))]
state = json.load(open(sys.argv[2]))
DEAD = re.compile(RewardsCfg().deadline_regex, re.I)
sel = {}
for e in ev:
    if e["type"] == "select":
        for m in e["markets"]: sel[m["slug"]] = m
t0, t1 = ev[0]["t"], ev[-1]["t"]
hours = (t1 - t0) / 3600
fills = [e for e in ev if e["type"] == "rfill" and e.get("material")]
hedges = [e for e in ev if e["type"] == "rhedge"]
print(f"run {hours:.1f} h; markets selected over the run: {len(sel)}; material fills {len(fills)} ({24 * len(fills) / hours:.0f} a day); hedges {len(hedges)}")
is_dead = lambda slug: bool(DEAD.search(slug.replace("-", " ")))
def grp(name, fs):
    if not fs: print(f"  {name:36s} 0"); return
    sh = sum(f["size"] for f in fs); usd = sum(f["size"] * f["price"] for f in fs)
    print(f"  {name:36s} fills {len(fs):3d}  shares {sh:6.0f}  ${usd:6.0f}  crossing book {sum(1 for f in fs if f.get('cross')):2d}  alone in the zone {sum(1 for f in fs if f.get('alone')):2d}")
print("fills by kind of question:")
grp("all", fills); grp("deadline question (now filtered out)", [f for f in fills if is_dead(f["key"])]); grp("other", [f for f in fills if not is_dead(f["key"])])
print("fills by how they came:")
grp("a print through or at our price", [f for f in fills if not f.get("cross")]); grp("a real order moved through our price", [f for f in fills if f.get("cross")])
# cost: hedges realised + open positions at the last mark
hl = collections.defaultdict(float)
for h in hedges: hl[h["key"]] += h["pnl"]
op = {}
for slug, p in (state.get("positions") or {}).items():
    mid = p.get("mid") if p.get("mid") is not None else 0.5
    pairs = min(p["yes"], p["no"])
    op[slug] = pairs + (p["yes"] - pairs) * mid + (p["no"] - pairs) * (1 - mid) - p["cost"]
tot_h, tot_o = sum(hl.values()), sum(op.values())
print(f"cost of the fills: hedges ${tot_h:+.2f} ({len(hedges)} exits, {sum(h['size'] for h in hedges):.0f} shares, {100 * -tot_h / max(sum(h['size'] for h in hedges), 1e-9):.1f}c a share); "
      f"positions still open ${tot_o:+.2f} at the last mid ({len(op)} markets)")
for name, f in (("deadline questions", is_dead), ("other questions", lambda s: not is_dead(s))):
    hh = sum(v for k, v in hl.items() if f(k)); oo = sum(v for k, v in op.items() if f(k)); n = len({x["key"] for x in fills if f(x["key"])})
    print(f"  {name:20s}: markets with fills {n:2d}  hedges ${hh:+7.2f}  open ${oo:+7.2f}  total ${hh + oo:+7.2f}")
st = [e for e in ev if e["type"] == "rstatus"][-1]
print(f"rewards by the book ${st['rewards_total']:.2f} (official ${st['official_total']:.2f}, alone ${st['alone_total']:.2f}); trading ${st['trading_pnl']:+.2f}; "
      f"per day: rewards ${24 * st['rewards_total'] / hours:.0f}, trading ${24 * st['trading_pnl'] / hours:+.0f}")
nd = len([s for s in sel if not is_dead(s)])
print(f"selected markets that are deadline questions: {len(sel) - nd} of {len(sel)}")
