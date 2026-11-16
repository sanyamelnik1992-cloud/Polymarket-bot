"""Real money of OUR wallet from the exchange's public record (trades + redeems), independent of the bots' journals.
usage: PMBOT_WALLET=<proxy wallet> python reports/77_tools/own_wallet.py OUT_DIR
Writes OUT_DIR/my_trades.json, my_taker.json, my_activity.json, per_market.json, lock_entries.json and prints:
the exchange's own PnL series (/v2/user-pnl), PnL per strategy (by market kind and start of the lock bots, 9 Nov 07:00 UTC)
and the lock entries by asset, price paid, size, second of the window, day and insurance.
The wallet address is read from the environment only: never write it into the repository."""
import calendar
import collections
import json
import os
import subprocess
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pmbot.fees import taker_fee  # noqa: E402
ME = os.environ.get("PMBOT_WALLET") or sys.exit("set PMBOT_WALLET")
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
LOCK_START = calendar.timegm((2026, 11, 9, 7, 0, 0))
def get(u):
    for k in range(3):
        r = subprocess.run(["curl", "-sS", "-m", "60", u], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(1)
    return {}
def paged(path):
    rows, cur = [], None
    for _ in range(40):
        u = f"https://data-api.polymarket.com{path}&limit=500" + (f"&cursor={cur}" if cur else "")
        j = get(u); d = j.get("data") or []; rows += d
        pg = j.get("pagination") or {}; cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur: break
    return rows
tr = paged(f"/v2/trades?user={ME}&taker_only=false"); tk = paged(f"/v2/trades?user={ME}&taker_only=true")
act = paged(f"/v2/activity?user={ME}")
for n, x in (("my_trades", tr), ("my_taker", tk), ("my_activity", act)):
    json.dump(x, open(os.path.join(OUT, n + ".json"), "w"))
p = (get(f"https://data-api.polymarket.com/v2/user-pnl?user={ME}&interval=all&fidelity=1d").get("data") or {}).get("points") or []
print("exchange's own PnL by day (economic = realised + unrealised):")
for x in p:
    print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(x['timestamp']))} economic {x['economic_pnl']:+8.2f} realised {x['realized_pnl']:+8.2f} "
          f"unrealised {x['unrealized_pnl']:+6.2f} fees {x['fees_paid']:+6.2f} rewards {x['reward_income']:.2f} trades {x['trade_count']}")
taker = {(t["transaction_hash"], t["token_id"], round(float(t["size"]), 4)) for t in tk}
m = collections.defaultdict(lambda: dict(cost=0.0, fee=0.0, sells=0.0, payout=0.0, buys=[], slug="", t=0))
for t in tr:
    x = m[t["condition_id"]]; sz, px = float(t["size"]), float(t["price"])
    is_tk = (t["transaction_hash"], t["token_id"], round(sz, 4)) in taker
    x["slug"] = t["slug"]; x["t"] = max(x["t"], int(t["timestamp"]))
    if t["side"] == "BUY":
        x["cost"] += sz * px; x["buys"].append((int(t["timestamp"]), t["outcome"], sz, px, is_tk))
        if is_tk and "updown" in t["slug"]: x["fee"] += taker_fee(sz, px)
    else:
        x["sells"] += sz * px
for a in act:
    if a["type"] == "REDEEM": m[a["condition_id"]]["payout"] += float(a["usdc_size"])
json.dump(m, open(os.path.join(OUT, "per_market.json"), "w"))
def kind(x):
    t0 = min(b[0] for b in x["buys"]) if x["buys"] else x["t"]
    if "updown" not in x["slug"]: return "rewards test (unresolved = 0)"
    if t0 < LOCK_START: return "old 5m maker (before lock)"
    return "15m lock" if "-15m-" in x["slug"] else "5m lock (L3 + L99)"
agg = collections.defaultdict(lambda: [0, 0, 0.0, 0.0])
for x in m.values():
    pnl = x["payout"] + x["sells"] - x["cost"] - x["fee"]; a = agg[kind(x)]
    a[0] += 1; a[1] += pnl < -0.01; a[2] += pnl; a[3] += x["cost"]
print("\nPnL per strategy (payout + sells - buys - estimated taker fee):")
for k, a in sorted(agg.items()):
    print(f"  {k:32s} markets {a[0]:4d} lost {a[1]:3d} pnl {a[2]:+8.2f} spent {a[3]:8.2f}")
E = []
for x in m.values():
    if "updown" not in x["slug"] or not x["buys"]: continue
    t0 = min(b[0] for b in x["buys"])
    if t0 < LOCK_START: continue
    start = int(x["slug"].rsplit("-", 1)[1]); first = sorted(x["buys"])[0][1]
    main = [b for b in x["buys"] if b[1] == first]; sh = sum(b[2] for b in main)
    E.append(dict(kind="15m" if "-15m-" in x["slug"] else "5m", asset=x["slug"].split("-")[0], sec=t0 - start, px=sum(b[2] * b[3] for b in main) / sh,
                  sh=sh, won=x["payout"] > 0.5 * sh, pnl=x["payout"] + x["sells"] - x["cost"] - x["fee"], hedged=len(main) < len(x["buys"]), t=t0))
json.dump(E, open(os.path.join(OUT, "lock_entries.json"), "w"))
def table(title, kind, buckets):
    sel = [e for e in E if e["kind"] == kind]
    print(f"\n{kind}: by {title}\n  {'':14s} {'n':>4s} {'lost':>4s} {'win%':>6s} {'avg px':>7s} {'pnl $':>8s} {'c/share':>8s}")
    for lab, f in buckets:
        s = [e for e in sel if f(e)]
        if not s: continue
        n = len(s); lost = sum(not e["won"] for e in s); sh = sum(e["sh"] for e in s); pnl = sum(e["pnl"] for e in s)
        print(f"  {lab:14s} {n:4d} {lost:4d} {100 * (n - lost) / n:6.1f} {sum(e['px'] * e['sh'] for e in s) / sh:7.3f} {pnl:+8.2f} {100 * pnl / sh:+8.2f}")
for k in ("5m", "15m"):
    table("asset", k, [(a, (lambda a: lambda e: e["asset"] == a)(a)) for a in ("btc", "eth", "sol", "xrp")])
    table("price paid", k, [("<0.88", lambda e: e["px"] < .88), ("0.88-0.94", lambda e: .88 <= e["px"] < .94), (">=0.94", lambda e: e["px"] >= .94)])
    table("size", k, [("<4", lambda e: e["sh"] < 4), ("4-8", lambda e: 4 <= e["sh"] < 8), (">=8", lambda e: e["sh"] >= 8)])
    d = 300 if k == "5m" else 900
    table("second", k, [(f"{a}-{a + d // 20}", (lambda a: lambda e: a <= e["sec"] < a + d // 20)(a)) for a in range(int(d * .8), d, d // 20)])
    table("day", k, [(dd, (lambda dd: lambda e: time.strftime('%m-%d', time.gmtime(e["t"])) == dd)(dd)) for dd in sorted({time.strftime('%m-%d', time.gmtime(e["t"])) for e in E})])
