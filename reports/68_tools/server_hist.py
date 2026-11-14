"""Hour-by-hour history of the server's rewards bot from the hourly reports on branch server-reports, plus every fill and
hedge the reports list (each report shows the last 15 fills and 10 hedges, so a busy hour can lose some).
Run in the repository after  git fetch origin server-reports:  python reports/68_tools/server_hist.py OUT_DIR
Writes OUT_DIR/hist.json (one row per report) and OUT_DIR/fh.json (fills, hedges)."""
import subprocess, re, json, sys
out = sys.argv[1] if len(sys.argv) > 1 else "."
log = subprocess.run(["git", "log", "--format=%h %ct", "--since=2026-11-12 08:00", "origin/server-reports"],
                     capture_output=True, text=True).stdout.split("\n")
num = r"\$?(-?[\d.]+)"
rows, fills, hedges = [], {}, {}
for ln in reversed([x for x in log if x]):
    c, ts = ln.split()
    t = subprocess.run(["git", "show", f"{c}:runs/pmbot-rewards.txt"], capture_output=True, text=True).stdout
    if not t.startswith("journal"):
        continue
    r = {"t": int(ts)}
    def g(key, rx, cast=float):
        m = re.search(rx, t)
        if m:
            r[key] = cast(m[1])
    g("markets", r"now: markets (\d+)", int); g("quoting", r"quoting (\d+)", int); g("bids", r"bids " + num)
    g("rate", r"pool rate at current shares " + num)
    g("rew_today", r"estimate by the book\): today " + num); g("rew", r"estimate by the book\): today \S+ total " + num)
    g("official", r"competitiveness: total " + num); g("alone", r"alone in a zone: total " + num); g("stress", r"identical maker\): total " + num)
    g("hedges", r"hedges: (\d+) for", int); g("hedge_sh", r"hedges: \d+ for ([\d.]+) shares"); g("hedge_lost", r"shares, lost " + num)
    g("unhedged", r"not hedged yet: " + num)
    g("value", r"positions: value " + num); g("cost", r"positions: value \S+ cost " + num); g("open", r"-> open " + num)
    g("realised", r"realised " + num); g("trading", r"trading " + num); g("trading_today", r"\(today (-?[\d.]+)\)")
    g("cautious", r"CAUTIOUS[^:]*: " + num); g("fills", r"fills total (\d+)", int)
    g("priced", r"last selection: \d+ markets of (\d+) priced", int); g("est", r"est " + num + "/day"); g("empty_books", r"empty books (\d+)", int)
    for key, name in (("m_all", "all"), ("m_alone", "mostly alone in the zone"), ("m_others", "with other makers in the zone"),
                      ("m_nofill", "never filled"), ("m_fill", "filled")):
        m = re.search(r"\n  " + name + r"\s+(\d+) markets  rewards \$\s*([\d.]+)  trading \$\s*(-?[\d.]+)  net \$\s*([+-]?[\d.]+)  fills\s+(\d+)  quoted\s+([\d.]+)", t)
        if m:
            r[key] = [int(m[1]), float(m[2]), float(m[3]), int(m[5]), float(m[6])]      # markets, rewards, trading, fills, hours
    r["days"] = re.findall(r"\n  (2026-\d\d-\d\d): book \$([\d.]+), official \$([\d.]+), alone \$([\d.]+) \| hedges (\d+) \(\$([\d.]+)\) \| trading (?:\$(-?[\d.]+)|day not closed)", t)
    rows.append(r)
    sec = None
    for line in t.splitlines():
        if line.startswith("fills:"): sec = "f"; continue
        if line.startswith("hedges:"): sec = "h"; continue
        if line.startswith("by market") or line.startswith("settlements"): sec = None; continue
        m = re.match(r"\s+(11-\d\d) (\d\d:\d\d) (\S+)\s+(YES|NO )\s+([\d.]+)@([\d.]+) last_trade=(\S+)( \(alone in the zone\))?", line)
        if sec == "f" and m:
            fills[(m[1], m[2], m[3], m[4], m[5])] = dict(d=m[1], t=m[2], slug=m[3], side=m[4].strip(), size=float(m[5]), price=float(m[6]),
                                                       last=None if m[7] == "None" else float(m[7]), alone=bool(m[8]))
        m = re.match(r"\s+(11-\d\d) (\d\d:\d\d) (\S+)\s+bought (YES|NO )\s+([\d.]+)@([\d.]+) held@([\d.]+) cost/share ([+-][\d.]+)", line)
        if sec == "h" and m:
            hedges[(m[1], m[2], m[3], m[4], m[5])] = dict(d=m[1], t=m[2], slug=m[3], buy=m[4].strip(), size=float(m[5]), price=float(m[6]),
                                                          held=float(m[7]), cost=float(m[8]))
json.dump(rows, open(f"{out}/hist.json", "w"))
json.dump({"fills": sorted(fills.values(), key=lambda f: (f["d"], f["t"])), "hedges": sorted(hedges.values(), key=lambda f: (f["d"], f["t"]))},
          open(f"{out}/fh.json", "w"))
print("reports", len(rows), "fills listed", len(fills), "of", rows[-1].get("fills"), "hedges listed", len(hedges), "of", rows[-1].get("hedges"))
