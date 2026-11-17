"""Market-wide: windows where takers bought a side at 0.98-0.995 X seconds before the end, and how often that side lost.
One example per window and time bucket (the first such buy)."""
import json, collections, sys
d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "mkt_taker.json"))   # reports/77_tools/mkt_map.py output
B5 = [(0, 30), (30, 60), (60, 120), (120, 180), (180, 300)]
B15 = [(0, 60), (60, 120), (120, 240), (240, 420), (420, 900)]
for lo_px, hi_px in ((0.98, 0.985), (0.985, 0.995)):
    for dur, B in ((300, B5), (900, B15)):
        res = collections.defaultdict(lambda: [0, 0, 0.0])
        for slug, v in d.items():
            if (dur == 300) != ("-5m-" in slug): continue
            end = int(slug.rsplit("-", 1)[1]) + dur
            first = {}
            for r in sorted(v["rows"], key=lambda r: r["t"]):
                if r["side"] != "BUY" or not (lo_px <= r["px"] < hi_px): continue
                left = end - r["t"]
                for b in B:
                    if b[0] <= left < b[1] and b not in first: first[b] = r
            for b, r in first.items():
                a = res[b]; a[0] += 1; a[1] += (r["o"] != v["win"]); a[2] += r["px"]
        print(f"{'5m' if dur == 300 else '15m'} px [{lo_px}, {hi_px}):", "  ".join(f"{b[0]}-{b[1]}s: {a[1]}/{a[0]} lost ({100*a[1]/max(1,a[0]):.1f}%)" for b, a in sorted(res.items())))
