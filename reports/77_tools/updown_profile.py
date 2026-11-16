"""How Up/Down wallets trade: maker vs taker buys, price buckets, part of the window elapsed.
usage: python updown_profile.py trades_crypto.json prof_crypto-month.json"""
import json, sys, collections, re, statistics
tr = json.load(open(sys.argv[1])); prof = {r["w"]: r for f in sys.argv[2].split(",") for r in json.load(open(f))}
def win_end(s):
    m = re.search(r"-updown-(5m|15m|4h)-(\d{10})$", s)
    if not m: return None, None
    d = {"5m": 300, "15m": 900, "4h": 14400}[m.group(1)]
    return m.group(1), int(m.group(2)) + d
for w, rows in sorted(tr.items(), key=lambda kv: -prof.get(kv[0], {}).get("pnl", 0)):
    ud = [(r, *win_end(r["s"])) for r in rows]
    ud = [(r, k, e) for r, k, e in ud if k]
    if len(ud) < 0.5 * len(rows) or not ud: continue
    p = prof.get(w, {})
    print(f"\n{w[:8]} pnl30 {p.get('pnl', 0):.0f} roi {100 * p.get('roi', 0):.1f}% trades30 {p.get('n', 0):.0f} rebate30 {p.get('reb', 0):.0f} days +{p.get('pos')}/-{p.get('neg')} maxDD {p.get('mdd', 0):.0f}")
    for role in (True, False):
        sel = [(r, k, e) for r, k, e in ud if r["tk"] == role and r["side"] == "BUY"]
        if len(sel) < 20: continue
        left = [e - r["t"] for r, k, e in sel]
        px = [r["px"] for r, k, e in sel]
        usd = sum(r["sz"] * r["px"] for r, k, e in sel)
        b = collections.Counter()
        for x in px:
            b["<.2" if x < .2 else ".2-.4" if x < .4 else ".4-.6" if x < .6 else ".6-.8" if x < .8 else ".8-.9" if x < .9 else ".9-.95" if x < .95 else ">=.95"] += 1
        lb = collections.Counter()
        for (r, k, e), l in zip(sel, left):
            d = 300 if k == "5m" else 900 if k == "15m" else 14400
            frac = 1 - l / d
            lb["0-25%" if frac < .25 else "25-50%" if frac < .5 else "50-75%" if frac < .75 else "75-90%" if frac < .9 else "90-100%" if frac <= 1 else "after"] += 1
        n = len(sel)
        print(f"  {'TAKER' if role else 'MAKER'} buys {n} ${usd:.0f} | price " + " ".join(f"{k}:{100 * v / n:.0f}" for k, v in sorted(b.items())) +
              " | window elapsed " + " ".join(f"{k}:{100 * v / n:.0f}" for k, v in sorted(lb.items())) + f" | kinds {dict(collections.Counter(k for r, k, e in sel))}")
    sells = sum(1 for r, k, e in ud if r["side"] == "SELL")
    print(f"  sells {sells} of {len(ud)}")
