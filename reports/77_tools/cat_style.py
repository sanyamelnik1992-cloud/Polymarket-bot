"""Group wallets by the category most of their $ goes to, with style: taker share, sells, buys >= 0.9, both outcomes.
usage: python cat_style.py trades_sys.json   (needs prof_all.json from prof_all.py)"""
import json, sys, collections, re, statistics
sys.path.insert(0, ".")
from catlib import cat
tr = json.load(open(sys.argv[1])); prof = {r["w"]: r for r in json.load(open("prof_all.json"))}
out = []
for w, rows in tr.items():
    if not rows: continue
    p = prof.get(w, {})
    vol = collections.Counter(); cnt = collections.Counter()
    for r in rows:
        c = cat(r["s"], r["title"]); vol[c] += r["sz"] * r["px"]; cnt[c] += 1
    tv = sum(vol.values()) or 1
    buys = [r for r in rows if r["side"] == "BUY"]
    tk = sum(r["tk"] for r in rows) / len(rows)
    sells = 1 - len(buys) / len(rows)
    hi = sum(1 for r in buys if r["px"] >= .9) / max(len(buys), 1)
    mk = collections.defaultdict(set)
    for r in buys: mk[r["s"]].add(r["o"])
    both = sum(1 for v in mk.values() if len(v) > 1) / max(len(mk), 1)
    main = vol.most_common(1)[0][0]
    out.append(dict(w=w, main=main, share=vol[main] / tv, tk=tk, sells=sells, hi=hi, both=both, mk=len(mk), n=len(rows), p=p,
                    kinds=" ".join(f"{k}:{100 * v / tv:.0f}" for k, v in vol.most_common(3))))
json.dump(out, open("cat_style.json", "w"))
groups = collections.defaultdict(list)
for o in out: groups[o["main"]].append(o)
for g, L in sorted(groups.items(), key=lambda kv: -sum(o["p"].get("pnl", 0) for o in kv[1])):
    tot = sum(o["p"].get("pnl", 0) for o in L)
    print(f"\n== {g}: {len(L)} wallets, pnl30 ${tot:,.0f}")
    for o in sorted(L, key=lambda o: -o["p"].get("pnl", 0))[:12]:
        p = o["p"]
        print(f"  {o['w'][:8]} pnl30 {p.get('pnl', 0):8.0f} roi {100 * p.get('roi', 0):5.2f}% trades30 {p.get('n', 0):7.0f} avg${p.get('avg', 0):5.0f} +{p.get('pos')}/-{p.get('neg')} reb {p.get('reb', 0):6.0f} | taker {100 * o['tk']:3.0f}% sells {100 * o['sells']:3.0f}% buys>=.9 {100 * o['hi']:3.0f}% bothSides {100 * o['both']:3.0f}% mkts {o['mk']:4d} | {o['kinds']}")
