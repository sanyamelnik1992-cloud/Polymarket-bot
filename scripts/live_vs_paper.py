"""Live bot vs its paper twin on the same 5m windows: what the paper simulation gets wrong.

  python scripts/live_vs_paper.py [--live data/journal-live] [--paper data/journal-live-twin] [--hours 24]

Prints: orders placed / rejected (by code) / API errors, place & cancel latency, fills and PnL per side of the pair
on common windows, fills that only one side got, and fill prices. Run on the server (journals live there).
"""
import argparse
import glob
import json
import os
import statistics
import time
from collections import Counter, defaultdict


def events(journal_dir, since):
    for run in sorted(glob.glob(os.path.join(journal_dir, "*", "events.jsonl"))):
        if os.path.getmtime(run) < since:
            continue
        with open(run) as f:
            for line in f:
                if '"type":"fv"' in line or '"type":"filters"' in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("t", 0) >= since:
                    yield e


def summarize(journal_dir, since):
    s = {"placed": 0, "rejected": Counter(), "errors": Counter(), "lat_place": [], "lat_cancel": [],
         "fills": defaultdict(list), "settle": {}, "health": Counter(), "kills": []}
    for e in events(journal_dir, since):
        t = e.get("type")
        if t == "quote":                       # engine: one per accepted order (paper and live)
            s["placed"] += 1
        elif t == "order_placed" and e.get("latency_ms") is not None:   # live broker only
            s["lat_place"].append(e["latency_ms"])
        elif t == "order_cancelled" and e.get("latency_ms") is not None:
            s["lat_cancel"].append(e["latency_ms"])
        elif t == "order_rejected":
            s["rejected"][e.get("code", "?")] += 1
        elif t in ("api_error", "order_error", "broker_fatal", "reset_failed", "user_stream_error", "heartbeat_error"):
            s["errors"][f"{t}:{e.get('op') or e.get('kind') or ''}"] += 1
        elif t == "broker_health" and not e.get("ok"):
            s["health"][e.get("reason")] += 1
        elif t == "fill":
            s["fills"][e["key"]].append((e["side"], e["price"], e["size"]))
        elif t == "settle":
            s["settle"][e["key"]] = e
        elif t == "kill":
            s["kills"].append(e.get("reason"))
    return s


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", default="data/journal-live")
    ap.add_argument("--paper", default="data/journal-live-twin")
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    since = time.time() - a.hours * 3600
    L, P = summarize(a.live, since), summarize(a.paper, since)
    print(f"=== live vs paper twin, last {a.hours:g} h")
    for name, s in (("live", L), ("paper", P)):
        pnl = sum(x["pnl"] for x in s["settle"].values())
        nf = sum(len(v) for v in s["fills"].values())
        print(f"{name:5s}: orders {s['placed']}, fills {nf} in {len(s['fills'])} windows, settled PnL {pnl:+.2f}$")
    if L["lat_place"]:
        print(f"place latency ms: median {statistics.median(L['lat_place']):.0f}, p90 {pct(L['lat_place'], .9):.0f}, "
              f"max {max(L['lat_place']):.0f}")
    if L["lat_cancel"]:
        print(f"cancel latency ms: median {statistics.median(L['lat_cancel']):.0f}, p90 {pct(L['lat_cancel'], .9):.0f}")
    print("rejected:", dict(L["rejected"]) or "none")
    print("errors:", dict(L["errors"]) or "none")
    print("no-quote time reasons (broker health):", dict(L["health"]) or "none")
    if L["kills"]:
        print("kills:", L["kills"])
    common = sorted(set(L["settle"]) & set(P["settle"]))
    only_l = sorted(set(L["fills"]) - set(P["fills"]))
    only_p = sorted(set(P["fills"]) - set(L["fills"]))
    print(f"\nwindows with fills: both {len(set(L['fills']) & set(P['fills']))}, live only {len(only_l)}, "
          f"paper only {len(only_p)}")
    print("(live only = we were filled at our price in the queue, which paper never counts; "
          "paper only = someone was ahead of us in the queue or our order was not there in time)")
    if common:
        dl = sum(L["settle"][k]["pnl"] for k in common)
        dp = sum(P["settle"][k]["pnl"] for k in common)
        print(f"common settled windows {len(common)}: live {dl:+.2f}$ vs paper {dp:+.2f}$")
    print("\nper window (key: live fills | paper fills):")
    for k in sorted(set(L["fills"]) | set(P["fills"]))[-30:]:
        fl = " ".join(f"{sd}{p:.2f}x{q:g}" for sd, p, q in L["fills"].get(k, [])) or "-"
        fp = " ".join(f"{sd}{p:.2f}x{q:g}" for sd, p, q in P["fills"].get(k, [])) or "-"
        pl = L["settle"].get(k, {}).get("pnl")
        pp = P["settle"].get(k, {}).get("pnl")
        print(f"  {k}: {fl:28s} | {fp:28s}  pnl {pl if pl is None else round(pl, 2)} / {pp if pp is None else round(pp, 2)}")


if __name__ == "__main__":
    main()
