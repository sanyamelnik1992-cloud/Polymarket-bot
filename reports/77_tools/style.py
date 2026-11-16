"""Style of wallets from their trades: market kinds by $, taker share, sells, buys >= 0.9 / <= 0.1, both outcomes, time left.
usage: python style.py trades_crypto.json prof_crypto-month.json"""
import json, sys, collections, re, statistics
tr = json.load(open(sys.argv[1])); prof = {r["w"]: r for f in sys.argv[2].split(",") for r in json.load(open(f))}
def kind(s):
    if re.search(r"-updown-5m-\d+$", s): return "5m"
    if re.search(r"-updown-15m-\d+$", s): return "15m"
    if re.search(r"-updown-4h-\d+$", s): return "4h"
    if re.search(r"up-or-down-.*-(\d+)(am|pm)-et$", s): return "1h"
    if "up-or-down-on-" in s: return "daily"
    if re.search(r"(bitcoin|ethereum|solana|xrp|btc|eth)-(above|below|reach|hit|price|dip)", s) or "what-price-will" in s: return "price"
    if any(x in s for x in ("bitcoin", "ethereum", "solana", "xrp", "crypto", "btc", "eth-")): return "crypto-other"
    return "non-crypto"
def end_of(s, k):
    m = re.search(r"-(\d{10})$", s)
    if not m: return None
    return int(m.group(1)) + {"5m": 300, "15m": 900, "4h": 14400}.get(k, 0)
res = []
for w, rows in tr.items():
    if not rows: continue
    p = prof.get(w, {})
    kc = collections.Counter(kind(r["s"]) for r in rows)
    vol = collections.Counter()
    for r in rows: vol[kind(r["s"])] += r["sz"] * r["px"]
    buys = [r for r in rows if r["side"] == "BUY"]
    tk = sum(r["tk"] for r in rows) / len(rows)
    sells = 1 - len(buys) / len(rows)
    pxs = [r["px"] for r in buys]
    hi = sum(1 for x in pxs if x >= 0.9) / max(len(pxs), 1)
    lo = sum(1 for x in pxs if x <= 0.1) / max(len(pxs), 1)
    # both outcomes bought in the same market
    mk = collections.defaultdict(set)
    for r in buys: mk[r["s"]].add(r["o"])
    both = sum(1 for v in mk.values() if len(v) > 1) / max(len(mk), 1)
    # seconds before the end of up/down windows
    left = []
    for r in buys:
        k = kind(r["s"])
        if k in ("5m", "15m", "4h"):
            e = end_of(r["s"], k)
            if e: left.append(e - r["t"])
    med_left = statistics.median(left) if left else None
    span = (max(r["t"] for r in rows) - min(r["t"] for r in rows)) / 3600
    top = kc.most_common(1)[0][0]
    res.append((p.get("pnl", 0), w, p, kc, vol, tk, sells, hi, lo, both, med_left, len(rows), span, statistics.median([r["sz"] * r["px"] for r in rows])))
res.sort(key=lambda x: -x[0])
print(f"{'pnl30':>8s} {'roi%':>6s} {'n30':>7s} {'main kinds (share of $)':38s} {'tk%':>4s} {'sell%':>5s} {'>=.9':>5s} {'<=.1':>5s} {'both':>5s} {'medLeft':>8s} {'med$':>6s}")
for pnl, w, p, kc, vol, tk, sells, hi, lo, both, ml, n, span, medv in res:
    tv = sum(vol.values())
    kinds = " ".join(f"{k}:{100 * v / tv:.0f}" for k, v in vol.most_common(3))
    print(f"{pnl:8.0f} {100 * p.get('roi', 0):6.1f} {p.get('n', 0):7.0f} {kinds:38s} {100 * tk:4.0f} {100 * sells:5.0f} {100 * hi:5.0f} {100 * lo:5.0f} {100 * both:5.0f} {('' if ml is None else f'{ml:.0f}'):>8s} {medv:6.0f}  {w[:8]}")
