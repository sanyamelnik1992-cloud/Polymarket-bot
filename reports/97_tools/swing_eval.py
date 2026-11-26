"""reports/97: what the "spike and fade" features of swing15.py say about the 15m Ступени entries.

  python swing_eval.py OUT.json [OUT2.json ...]

Per share: won -> 1 - price - fee, lost -> -price - fee, fee = 0.05 x price x (1 - price) (the exchange's fd.r of the crypto
Up/Down markets). A filter is judged by the entries it removes: how many of them lost, and the money of the kept ones."""
import json
import sys
from collections import defaultdict

FEE = lambda p: 0.05 * p * (1 - p)                    # noqa: E731


def load(paths):
    E = []
    for p in paths:
        E += json.load(open(p))["entries"]
    for e in E:
        e["pnl"] = (1.0 - e["px"] if e["won"] else -e["px"]) - FEE(e["px"])
    return E


def row(label, es, base=None):
    n = len(es)
    if not n:
        return f"  {label:46s}     0"
    lost = sum(not e["won"] for e in es)
    pnl = sum(e["pnl"] for e in es)
    px = sum(e["px"] for e in es) / n
    be = 100 * (1 - px)
    return (f"  {label:46s} {n:5d}  lost {lost:3d} ({100 * lost / n:4.1f} %, break-even {be:4.1f} %)  "
            f"{100 * pnl / n:+6.2f} c a share  ${pnl * 10:+7.2f} at 10 shares")


def bins(E, key, edges, label):
    print(f"\n{label}:")
    for lo, hi in zip(edges[:-1], edges[1:]):
        es = [e for e in E if e.get(key) is not None and lo <= e[key] < hi]
        print(row(f"{key} [{lo:g}, {hi:g})", es))


def main(paths):
    E = load(paths)
    print(row("ALL entries", E))
    for a in sorted({e["a"] for e in E}):
        print(row(f"  {a}", [e for e in E if e["a"] == a]))
    for d in sorted({e["day"] for e in E}):
        print(row(f"  day {d}", [e for e in E if e["day"] == d]))
    print("\nby tier price:")
    for lo, hi in ((0.90, 0.915), (0.915, 0.935), (0.935, 0.955), (0.955, 0.99)):
        print(row(f"price [{lo}, {hi})", [e for e in E if lo <= e["px"] < hi]))
    bins(E, "min_lead_120", [-1e9, -5, -2, 0, 2, 5, 1e9], "lowest lead of the last 120 s (bp; < 0 = the price was on the other side)")
    bins(E, "min_lead_60", [-1e9, -2, 0, 2, 5, 1e9], "lowest lead of the last 60 s")
    bins(E, "run_60", [-1e9, 0, 3, 6, 10, 1e9], "move toward our side over the last 60 s (bp)")
    bins(E, "run_120", [-1e9, 0, 3, 6, 10, 15, 1e9], "move toward our side over the last 120 s (bp)")
    for e in E:
        e["swing_rel"] = e["swing_120"] / max(e["lead_bp"], 0.1)
        e["run_rel"] = e["run_120"] / max(e["lead_bp"], 0.1)
    bins(E, "swing_rel", [0, 1, 1.5, 2, 3, 5, 1e9], "swing of 120 s / lead")
    bins(E, "run_rel", [-1e9, 0, 0.5, 1, 1.5, 2, 1e9], "move of 120 s toward us / lead (> 1 = the whole lead came in the last 2 minutes)")
    print("\nthe lead K seconds before the entry (< 0 = our side was losing then):")
    for k in (30, 60, 90, 120, 180, 240):
        key = f"lead_at_{k}"
        if key not in E[0]:
            continue
        neg = [e for e in E if e[key] < 0]
        pos = [e for e in E if e[key] >= 0]
        print(row(f"lead {k:3d} s before < 0", neg))
        print(row(f"lead {k:3d} s before >= 0", pos))
    print("\nfilters (removed entries):")
    F = [("crossed in the last 60 s (min_lead_60 < 0)", lambda e: e["min_lead_60"] < 0),
         ("crossed in the last 120 s (min_lead_120 < 0)", lambda e: e["min_lead_120"] < 0),
         ("120 s move toward us > lead (run_rel > 1)", lambda e: e["run_rel"] > 1),
         ("120 s move toward us > 1.5 x lead", lambda e: e["run_rel"] > 1.5),
         ("120 s swing > 2 x lead", lambda e: e["swing_rel"] > 2),
         ("120 s swing > 3 x lead", lambda e: e["swing_rel"] > 3),
         ("crossed in 120 s AND run_rel > 1", lambda e: e["min_lead_120"] < 0 and e["run_rel"] > 1)]
    for k in (60, 90, 120, 180):
        if f"lead_at_{k}" in E[0]:
            F.append((f"our side was losing {k} s before (lead_at_{k} < 0)", lambda e, k=k: e[f"lead_at_{k}"] < 0))
            F.append((f"losing {k} s before by more than the lead now", lambda e, k=k: e[f"lead_at_{k}"] < -e["lead_bp"]))
    for label, f in F:
        rem = [e for e in E if f(e)]
        kept = [e for e in E if not f(e)]
        days = sorted({e["day"] for e in E})
        half = days[len(days) // 2]
        by_half = []
        for part in ([d for d in days if d < half], [d for d in days if d >= half]):
            r = [e for e in rem if e["day"] in part]
            by_half.append(f"{len(r)}/{sum(not e['won'] for e in r)}/{sum(e['pnl'] for e in r) * 10:+.2f}$")
        print(f"\n  {label}")
        print(row("removed", rem))
        print(row("kept", kept))
        print(f"    removed in the first / second half of the days (entries/lost/$ at 10 sh): {by_half[0]} | {by_half[1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
