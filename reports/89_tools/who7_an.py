import json, re, collections, time
raw = json.load(open("who7_raw.json"))
prof = {r["w"]: r for r in json.load(open("prof7.json"))}
def kind(slug):
    s = slug or ""
    m = re.match(r"(btc|eth|sol|xrp|doge|bnb|hype)-updown-(5m|15m|4h)-", s)
    if m: return m.group(2)
    if re.match(r"(bitcoin|ethereum|solana|xrp|dogecoin|bnb|hyperliquid)-up-or-down-", s): return "1h/d"
    if "updown" in s or "up-or-down" in s: return "ud?"
    return "oth"
print(f"{'wallet':10s} {'pnl7':>7s} {'reb':>6s} {'fees':>6s} {'trades7':>7s} {'span_h':>6s} {'mk%':>4s} {'kinds (share of last trades)':40s} {'buy%':>4s} {'avgpx':>5s} {'med$':>5s} {'acts':30s}")
res = []
for w, d in raw.items():
    a, t = d["all"], d["taker"]
    if not a: continue
    tkeys = collections.Counter((x["transaction_hash"], x["side"], x["outcome"], round(float(x["size"]), 4)) for x in t)
    tmin = min(int(x["timestamp"]) for x in a)
    # maker rows: rows of `all` not matched by a taker row, restricted to the time span the taker list also covers
    tspan = min(int(x["timestamp"]) for x in t) if t else time.time()
    lo = max(tmin, tspan)
    aa = [x for x in a if int(x["timestamp"]) >= lo]
    mk = 0
    left = tkeys.copy()
    for x in aa:
        k = (x["transaction_hash"], x["side"], x["outcome"], round(float(x["size"]), 4))
        if left.get(k):
            left[k] -= 1
        else:
            mk += 1
    span = (max(int(x["timestamp"]) for x in a) - tmin) / 3600
    kinds = collections.Counter(kind(x.get("slug") or x.get("event_slug")) for x in a)
    tot = sum(kinds.values())
    ks = " ".join(f"{k}:{100 * v / tot:.0f}" for k, v in kinds.most_common(4))
    buys = sum(x["side"] == "BUY" for x in a)
    px = sum(float(x["price"]) for x in a) / len(a)
    usd = sorted(float(x["price"]) * float(x["size"]) for x in a)
    acts = collections.Counter(x.get("type") for x in d["act"])
    ac = " ".join(f"{k[:5]}:{v}" for k, v in acts.most_common(4))
    p = prof[w]
    res.append((p["pnl"], w))
    print(f"{w[:10]} {p['pnl']:7.0f} {p['reb']:6.0f} {p['fees']:6.0f} {p['n']:7.0f} {span:6.1f} {100 * mk / max(len(aa), 1):4.0f} {ks:40s} {100 * buys / len(a):4.0f} {px:5.2f} {usd[len(usd) // 2]:5.1f} {ac:30s}")
