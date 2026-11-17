"""Profile of the competitor's entries: kind, seconds before the end, price, size, outcome, concurrency."""
import json, collections, time, statistics as st, re, sys
D = json.load(open("comp.json")); rows, O = D["rows"], D["oc"]
def fee(p): return 0.07 * p * (1 - p)
def kind_end(s):
    m = re.match(r"(\w+)-updown-(5m|15m|4h)-(\d+)$", s)
    if m:
        dur = {"5m": 300, "15m": 900, "4h": 14400}[m.group(2)]
        return m.group(2), m.group(1), int(m.group(3)) + dur, dur
    # hourly: "<asset>-up-or-down-november-9-2026-10am-et": the hour starting at 10am ET
    m = re.match(r"(\w+)-up-or-down-(\w+)-(\d+)-(\d+)-(\d+)(am|pm)-et$", s)
    if m:
        asset = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp"}.get(m.group(1), m.group(1))
        mon = {"november": 11}[m.group(2)]; day = int(m.group(3)); yr = int(m.group(4)); h = int(m.group(5)) % 12 + (12 if m.group(6) == "pm" else 0)
        import calendar
        st_utc = calendar.timegm((yr, mon, day, h, 0, 0)) + 5 * 3600     # ET = UTC-5 in November
        return "1h", asset, st_utc + 3600, 3600
    return None, None, None, None
E = []
for r in rows:
    k, a, end, dur = kind_end(r["s"])
    won = O[r["s"]]["win"] == r["o"]
    E.append(dict(r, kind=k, asset=a, left=end - r["t"], dur=dur, won=won, cost=r["sz"] * r["px"], pnl=(r["sz"] if won else 0) - r["sz"] * r["px"] - r["sz"] * fee(r["px"])))
json.dump(E, open("comp_entries.json", "w"))
print("entries", len(E), "lost", sum(not e["won"] for e in E), "cost $%.0f" % sum(e["cost"] for e in E), "pnl after fee $%.2f" % sum(e["pnl"] for e in E),
      "fees $%.2f" % sum(e["sz"] * fee(e["px"]) for e in E))
print("first", time.strftime("%m-%d %H:%M", time.gmtime(E[0]["t"])), "last", time.strftime("%m-%d %H:%M", time.gmtime(E[-1]["t"])))
by = collections.defaultdict(list)
for e in E: by[e["kind"]].append(e)
print("\nkind  n  lost  cost   pnl   avg$  avgpx  left(s): p10 med p90")
for k, es in sorted(by.items(), key=lambda x: -len(x[1])):
    L = sorted(e["left"] for e in es)
    print(f"{k:4s} {len(es):3d} {sum(not e['won'] for e in es):3d} {sum(e['cost'] for e in es):7.0f} {sum(e['pnl'] for e in es):+7.2f} {st.mean(e['cost'] for e in es):6.1f} {sum(e['cost'] for e in es)/sum(e['sz'] for e in es):.4f}  {L[len(L)//10]:5.0f} {L[len(L)//2]:5.0f} {L[9*len(L)//10]:5.0f}")
print("\nby asset:", {a: (len(v), sum(not e['won'] for e in v), round(sum(e['pnl'] for e in v), 2)) for a, v in sorted(collections.defaultdict(list, {a: [e for e in E if e['asset'] == a] for a in {e['asset'] for e in E}}).items())})
print("\nprice histogram:", sorted(collections.Counter(round(e["px"], 2) for e in E).items()))
print("size histogram (shares, rounded):", collections.Counter(int(round(e["sz"], -1)) for e in E).most_common(12))
print("\nLOSSES:")
for e in E:
    if not e["won"]:
        print(" ", time.strftime("%m-%d %H:%M:%S", time.gmtime(e["t"])), e["s"], e["o"], e["sz"], e["px"], "left %ds" % e["left"], "lost $%.2f" % e["cost"])
# concurrency: how many entries share the same close time
ends = collections.Counter((e["t"] + e["left"]) for e in E)
print("\nentries per close time:", collections.Counter(ends.values()))
