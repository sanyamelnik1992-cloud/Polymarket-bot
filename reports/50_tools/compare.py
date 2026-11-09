"""List replay entries per rule with UTC times; compare with the paper bots' settles (server-reports settles.csv)."""
import collections, csv, json, sys, time
fee = lambda p: 0.07 * p * (1 - p)
C = []
for f in sys.argv[2:]:
    C += json.load(open(f))["cands"]
def tier(*tt, floor=0.90):
    return lambda p, px: px >= floor and any(p >= pm and px <= am for pm, am in tt)
RULES = {
    "L": lambda p, px: p >= 0.99 and 0.90 <= px <= min(0.98, p - 0.005 - fee(px)),
    "L3": tier((.99, .98), (.97, .95), (.95, .93), (.90, .91)),
    "L4": tier((.99, .98), (.97, .95), (.95, .93), (.90, .91), floor=0.50),
}
by = collections.defaultdict(list)
for c in C: by[(c["asset"], c["ws"])].append(c)
for v in by.values(): v.sort(key=lambda c: c["s"])
def first(rule):
    out = {}
    for k, v in by.items():
        for c in v:
            if rule(c["p"], c["px"]): out[k] = c; break
    return out
paper = collections.defaultdict(dict)
for r in csv.DictReader(open(sys.argv[1])):
    if r["unit"] in ("pmbot-lock", "pmbot-lock-tier", "pmbot-lock-live"):
        a, ws = r["key"].rsplit("-", 1)
        paper[r["unit"]][(a, int(ws))] = r
hm = lambda t: time.strftime("%H:%M", time.gmtime(t))
for name, rule in RULES.items():
    e = first(rule)
    print(f"\n== {name}: {len(e)} replay entries, lost {sum(not c['won'] for c in e.values())}")
    for (a, ws), c in sorted(e.items(), key=lambda kv: kv[0][1]):
        pl = 5 * ((1 - c['px']) if c['won'] else -c['px']) - 5 * fee(c['px'])
        tags = [u.replace("pmbot-", "") for u in paper if (a, ws) in paper[u]]
        print(f"  {hm(ws)} {a} s={c['s']} p={c['p']:.4f} px={c['px']:.3f} {'won ' if c['won'] else 'LOST'} {pl:+.2f}  paper:{','.join(tags) or '-'}")
for u in paper:
    print(f"\n{u}: paper entries today:", ", ".join(f"{hm(ws)} {a}" for (a, ws) in sorted(paper[u], key=lambda k: k[1]) if ws >= 1794182400))
