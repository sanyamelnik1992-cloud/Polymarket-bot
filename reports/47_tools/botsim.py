"""Bot-level simulation of entry rules on candidate lists: one entry per window and coin, at most $10 open (the live limit),
daily stop at -$5 (realized at window end + ~2 s), 5 shares. Reports per rule: entries, losses, P&L, days, worst day,
days stopped, and the same without XRP. usage: botsim.py CANDS.json [CANDS.json ...]"""
import collections, json, sys
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[1:]:
    C += json.load(open(f))["cands"]
def tier(*tt, floor=0.90):
    return lambda p, px: px >= floor and any(p >= pm and px <= am for pm, am in tt)
RULES = {
    "L сейчас": lambda p, px: p >= 0.99 and 0.90 <= px <= min(0.98, p - 0.005 - fee(px)),
    "ступени B": tier((.99, .98), (.97, .95), (.95, .93), (.90, .91)),
    "ступени A": tier((.99, .98), (.97, .96), (.95, .94), (.90, .92)),
    "B + широкий (цена от 0.50)": tier((.99, .98), (.97, .95), (.95, .93), (.90, .91), floor=0.50),
    "B + широкий до 0.80 (T4 → 0.80)": lambda p, px: (p >= 0.90 and 0.50 <= px <= 0.80) or tier((.99, .98), (.97, .95), (.95, .93), (.90, .91))(p, px),
    "только широкий W (p≥.90, 0.50–0.80)": lambda p, px: p >= 0.90 and 0.50 <= px <= 0.80,
    "запас 3¢": lambda p, px: p >= 0.90 and 0.90 <= px <= p - 0.03,
    "запас 4¢": lambda p, px: p >= 0.90 and 0.90 <= px <= p - 0.04,
    "дёшево ≤0.93": tier((.90, .93)),
}
def first_entries(rule, skip=()):
    by = collections.defaultdict(list)
    for c in C:
        if c["asset"] in skip: continue
        by[(c["asset"], c["ws"])].append(c)
    out = []
    for v in by.values():
        v.sort(key=lambda c: c["s"])
        for c in v:
            if rule(c["p"], c["px"]): out.append(c); break
    return out
def pnl(c): return 5 * ((1 - c["px"]) if c["won"] else -c["px"]) - 5 * fee(c["px"])
def simulate(ent, cap=10.0, stop=5.0):
    ev = sorted(ent, key=lambda c: c["ws"] + c["s"])
    days = collections.defaultdict(lambda: {"n": 0, "lost": 0, "pnl": 0.0, "stopped": False, "skipped": 0})
    open_ = []                      # (end_time, cost)
    realized = collections.defaultdict(float); pending = []   # pending settlements: (settle_time, day, pnl)
    for c in ev:
        t = c["ws"] + c["s"]; day = c["ws"] // 86400
        # settle everything that ended before t
        pending.sort()
        while pending and pending[0][0] <= t:
            st, d, pn = pending.pop(0); realized[d] += pn
        open_ = [(e, k) for e, k in open_ if e > t]
        if days[day]["stopped"] or realized[day] <= -stop:
            days[day]["stopped"] = True; days[day]["skipped"] += 1; continue
        cost = 5 * c["px"] + 5 * fee(c["px"])
        if sum(k for _, k in open_) + cost > cap + 1e-9:
            days[day]["skipped"] += 1; continue
        open_.append((c["ws"] + 300, cost)); pending.append((c["ws"] + 302, day, pnl(c)))
        d = days[day]; d["n"] += 1; d["lost"] += 0 if c["won"] else 1; d["pnl"] += pnl(c)
    return days
print(f"{'правило':14s} {'вход.':>5s} {'проигр.':>7s} {'P&L $':>8s} {'дней':>4s} {'ср.день':>8s} {'худший день':>11s} {'дней со стопом':>14s} {'пропущено лимитом':>17s}")
for skip, label in ((("",), "все монеты"), (("xrp",), "без XRP")):
    print(f"--- {label}")
    for name, rule in RULES.items():
        d = simulate(first_entries(rule, skip))
        n = sum(x["n"] for x in d.values()); lost = sum(x["lost"] for x in d.values()); tot = sum(x["pnl"] for x in d.values())
        worst = min(x["pnl"] for x in d.values()); stopped = sum(x["stopped"] for x in d.values()); sk = sum(x["skipped"] for x in d.values())
        print(f"{name:14s} {n:5d} {lost:7d} {tot:+8.1f} {len(d):4d} {tot/len(d):+8.1f} {worst:+11.1f} {stopped:14d} {sk:17d}")
