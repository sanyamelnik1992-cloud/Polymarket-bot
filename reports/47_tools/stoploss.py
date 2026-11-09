"""Would an early exit limit the loss of the lock entries? For every L3 (tier B) entry of the 96 h set + today's out-of-sample set:
token price path after the entry from the public tape (1 s VWAP buckets, complementary-token trades mirrored). Stop at level S: when the
bucket price <= S, sell at the NEXT second's price (1 s exit delay, pessimistic) and pay the taker fee on the sale.
usage: stoploss.py DATA_DIR
Compares P&L of hold-to-resolution with P&L of stop-S, and counts false stops (winners that dipped below S)."""
import collections, json, sys
S_DIR = (sys.argv[1] if len(sys.argv) > 1 else ".").rstrip("/") + "/"      # folder with the *.jsonl windows and cands_*.json
fee = lambda p: 0.07 * p * (1 - p)
other = lambda o: "Down" if o == "Up" else "Up"
sets = {"btc": (["btc_prev3d.jsonl", "btc_24h.jsonl", "btc_oos.jsonl"], ["cands_btc.json", "cands_oos_btc.json"]),
        "eth": (["eth_prev3d.jsonl", "eth_24h.jsonl", "eth_oos.jsonl"], ["cands_eth.json", "cands_oos_eth.json"]),
        "sol": (["sol_4d.jsonl", "sol_oos.jsonl"], ["cands_sol.json", "cands_oos_sol.json"]),
        "xrp": (["xrp_4d.jsonl", "xrp_oos.jsonl"], ["cands_xrp.json", "cands_oos_xrp.json"])}
B = lambda p, px: px >= 0.90 and any(p >= pm and px <= am for pm, am in ((.99, .98), (.97, .95), (.95, .93), (.90, .91)))
entries = {}
for a, (wf, cf) in sets.items():
    by = collections.defaultdict(list)
    for f in cf:
        for c in json.load(open(S_DIR + f))["cands"]: by[c["ws"]].append(c)
    for ws, v in by.items():
        v.sort(key=lambda c: c["s"])
        for c in v:
            if B(c["p"], c["px"]): entries[(a, ws)] = c; break
print("входов L3:", len(entries), " проигрышей:", sum(not c["won"] for c in entries.values()))
LEVELS = [0.85, 0.80, 0.70, 0.60, 0.50, 0.40]
paths = {}
for a, (wf, cf) in sets.items():
    for f in wf:
        for line in open(S_DIR + f):
            w = json.loads(line); key = (a, w["ws"])
            c = entries.get(key)
            if not c or w["winner"] not in ("Up", "Down"): continue
            tok = w["winner"] if c["won"] else other(w["winner"])
            buckets = collections.defaultdict(lambda: [0.0, 0.0])
            for (ts, wal, side, out, size, p, tk) in w["trades"]:
                r = ts - w["ws"]
                if r < c["s"] + 2 or r > 299: continue
                pt = p if out == tok else 1 - p
                b = buckets[r]; b[0] += pt * size; b[1] += size
            paths[key] = sorted((r, b[0] / b[1]) for r, b in buckets.items() if b[1] > 0)
print("входов с путём цены:", len(paths))
def hold(c): return 5 * ((1 - c["px"]) if c["won"] else -c["px"]) - 5 * fee(c["px"])
def run(S):
    tot = 0.0; stopped_w = stopped_l = 0; wins = losses = 0; ex_l = []; lost_gain = 0.0
    for key, c in entries.items():
        path = paths.get(key)
        h = hold(c); res = h
        if path:
            for i, (r, v) in enumerate(path):
                if v <= S:
                    nxt = next((vv for rr, vv in path[i + 1:] if rr >= r + 1), None)
                    ex = nxt if nxt is not None else max(v - 0.05, 0.01)
                    res = 5 * (ex - c["px"]) - 5 * fee(c["px"]) - 5 * fee(ex)
                    if c["won"]: stopped_w += 1; lost_gain += h - res
                    else: stopped_l += 1; ex_l.append(ex)
                    break
        tot += res
    return tot, stopped_w, stopped_l, ex_l, lost_gain
h = sum(hold(c) for c in entries.values())
nw = sum(c["won"] for c in entries.values()); nl = len(entries) - nw
print(f"\nдержим до конца: P&L {h:+.1f}$ ({nl} проигрышей по ~$4.6)")
print(f"{'стоп на цене':>12s} {'P&L':>8s} {'разница':>8s} {'ложных стопов (выигравших сняли)':>34s} {'проигравших сняли':>18s} {'ср. цена продажи проигравших':>30s}")
for S in LEVELS:
    t, sw, sl, ex_l, lg = run(S)
    print(f"{S:12.2f} {t:+8.1f} {t-h:+8.1f} {sw:20d} из {nw:3d} {sl:12d} из {nl:2d} {(sum(ex_l)/len(ex_l) if ex_l else 0):18.2f}")
