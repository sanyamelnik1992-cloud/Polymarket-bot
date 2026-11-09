"""Apply entry rules to candidate lists. usage: tiers.py CANDS.json [CANDS.json ...]
A rule is a function (p, px) -> bool; the first candidate per window (in time) that satisfies it is the entry.
P&L per entry = 5 shares x (win - price) - taker fee. Reports totals, per coin and per day (UTC)."""
import collections, json, sys
fee = lambda p: 0.07 * p * (1 - p)
C, nwin = [], {}
for f in sys.argv[1:]:
    d = json.load(open(f)); C += d["cands"]; nwin[d["asset"]] = d["windows"]
def L_live(p, px):  return p >= 0.99 and 0.90 <= px <= min(0.98, p - 0.005 - fee(px))
def tier(*tt):      # tt = ((p_min, ask_max), ...)
    def r(p, px):
        if px < 0.90: return False
        return any(p >= pm and px <= am for pm, am in tt)
    return r
RULES = {
    "L сейчас (p≥.99, ask≤.98)": L_live,
    "L: p≥.98, ask≤.98": lambda p, px: p >= 0.98 and 0.90 <= px <= 0.98,
    "ступени A: .99→.98 | .97→.96 | .95→.94 | .90→.92": tier((.99, .98), (.97, .96), (.95, .94), (.90, .92)),
    "ступени B: .99→.98 | .97→.95 | .95→.93 | .90→.91": tier((.99, .98), (.97, .95), (.95, .93), (.90, .91)),
    "ступени C (строже): .99→.98 | .97→.94 | .95→.92": tier((.99, .98), (.97, .94), (.95, .92)),
    "только дешёвые: любой p≥.90, ask≤.93": tier((.90, .93)),
    "только дешёвые: любой p≥.90, ask≤.92": tier((.90, .92)),
    "запас 3¢: ask ≤ p−.03 (p≥.90)": lambda p, px: p >= 0.90 and 0.90 <= px <= p - 0.03,
    "запас 5¢: ask ≤ p−.05 (p≥.90)": lambda p, px: p >= 0.90 and 0.90 <= px <= p - 0.05,
}
bywin = collections.defaultdict(list)
for c in C: bywin[(c["asset"], c["ws"])].append(c)
for v in bywin.values(): v.sort(key=lambda c: c["s"])
def run(rule, filt=lambda c: True):
    ent = []
    for k, v in bywin.items():
        for c in v:
            if filt(c) and rule(c["p"], c["px"]):
                ent.append(c); break
    return ent
def summ(ent):
    if not ent: return "входов 0"
    pnl = sum(5 * ((1 - c["px"]) if c["won"] else -c["px"]) - 5 * fee(c["px"]) for c in ent)
    lost = sum(1 for c in ent if not c["won"])
    return f"входов {len(ent):3d}, проигр. {lost}, P&L {pnl:+7.2f}$ ({100*pnl/(5*len(ent)):+5.2f}¢/акц), ср. цена {sum(c['px'] for c in ent)/len(ent):.3f}"
tot = sum(nwin.values())
print(f"окон всего {tot}: " + ", ".join(f"{a} {n}" for a, n in nwin.items()))
for name, rule in RULES.items():
    ent = run(rule)
    print(f"\n{name}\n   всего: {summ(ent)}  (на 1000 окон: {1000*len(ent)/tot:.0f} входов)")
    for a in nwin:
        print(f"   {a:4s}: {summ([c for c in ent if c['asset'] == a])}")
    days = sorted({c['ws'] // 86400 for c in ent})
    print("   по дням: " + " | ".join(f"д{d%100}: {summ([c for c in ent if c['ws']//86400==d]).split(', P&L')[0]}, P&L {sum(5*((1-c['px']) if c['won'] else -c['px'])-5*fee(c['px']) for c in ent if c['ws']//86400==d):+.1f}" for d in days))
