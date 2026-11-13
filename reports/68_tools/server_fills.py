"""All fills and hedges of the server's rewards bot from the hourly reports (each report lists the last 15 of each), by kind
of market. Run in the repository after  git fetch origin server-reports.  Writes fh.json next to the working directory."""
import subprocess, re, json, collections, statistics
cs = subprocess.run(["git", "log", "--format=%h", "--since=2026-11-12 08:00", "origin/server-reports"], capture_output=True, text=True).stdout.split()
fills, hedges = {}, {}
for c in cs:
    t = subprocess.run(["git", "show", f"{c}:runs/pmbot-rewards.txt"], capture_output=True, text=True).stdout
    sec = None
    for line in t.splitlines():
        if line.startswith("fills:"): sec = "f"; continue
        if line.startswith("hedges:"): sec = "h"; continue
        if line.startswith("by market") or line.startswith("settlements"): sec = None; continue
        m = re.match(r"\s+(11-1\d) (\d\d:\d\d) (\S+)\s+(YES|NO )\s+([\d.]+)@([\d.]+) last_trade=(\S+)( \(alone in the zone\))?", line)
        if sec == "f" and m:
            fills[(m[1], m[2], m[3], m[4], m[5])] = dict(d=m[1], t=m[2], slug=m[3], side=m[4].strip(), size=float(m[5]), price=float(m[6]),
                                                       last=None if m[7] == "None" else float(m[7]), alone=bool(m[8]))
        m = re.match(r"\s+(11-1\d) (\d\d:\d\d) (\S+)\s+bought (YES|NO )\s+([\d.]+)@([\d.]+) held@([\d.]+) cost/share ([+-][\d.]+)", line)
        if m and sec != "f":
            hedges[(m[1], m[2], m[3], m[4])] = dict(d=m[1], t=m[2], slug=m[3], buy=m[4].strip(), size=float(m[5]), price=float(m[6]), held=float(m[7]), cost=float(m[8]))
json.dump({"fills": list(fills.values()), "hedges": list(hedges.values())}, open("fh.json", "w"))
print("fills", len(fills), "hedges", len(hedges))
def cat(s):
    rules = [("weather", r"temperature|rain|snow"), ("2028 election by state", r"win-[a-z-]+-in-the-2028|2028-presidential"),
             ("sports season / player", r"nba-|nfl|dst|play-for|mvp|world-cup|steelers|esports|-win-the-20"),
             ("AI / tech", r"ai-|anthropic|openai|gpt|gemini|codex|openrouter|model|grok|meta-|microsoft|google|nvidia"),
             ("econ / rates / prices", r"rate|inflation|cpi|gdp|yield|treasury|gas-hit|ornn|index|home-value|comparable|sales|jobs|bank-of"),
             ("culture / media", r"netflix|billboard|song|movie|box-office|voice|miss-universe|grammy|oscar|rothko|auction"),
             ("politics / geo", r"trump|russia|iran|ukraine|china|election|president|minister|white-house|ceasefire|kushner")]
    for n, rx in rules:
        if re.search(rx, s): return n
    return "other"
hs = collections.defaultdict(list)
for h in hedges.values(): hs[h["slug"]].append(h)
agg = collections.defaultdict(lambda: dict(fills=0, sh=0.0, alone=0, hedged_sh=0.0, hcost=0.0, slugs=set()))
for f in fills.values():
    a = agg[cat(f["slug"])]; a["fills"] += 1; a["sh"] += f["size"]; a["alone"] += f["alone"]; a["slugs"].add(f["slug"])
for slug, hl in hs.items():
    a = agg[cat(slug)]
    for h in hl: a["hedged_sh"] += h["size"]; a["hcost"] += h["size"] * h["cost"]
print(f"{'category':26s} {'fills':>5s} {'alone':>5s} {'shares':>7s} {'hedged':>7s} {'unhedged':>8s} {'hedge c/sh':>10s} {'markets':>7s}")
for k, a in sorted(agg.items(), key=lambda x: -x[1]["fills"]):
    print(f"{k:26s} {a['fills']:5d} {a['alone']:5d} {a['sh']:7.0f} {a['hedged_sh']:7.0f} {max(a['sh'] - a['hedged_sh'], 0):8.0f} {100 * a['hcost'] / max(a['hedged_sh'], 1e-9):10.1f} {len(a['slugs']):7d}")
# fills relative to the last trade
gaps = []
for f in fills.values():
    if f["last"] is None: continue
    y = f["price"] if f["side"] == "YES" else 1 - f["price"]
    gaps.append((f["last"] - y) if f["side"] == "YES" else (y - f["last"]))
import numpy as np
g = np.array(gaps)
print("fill price vs the trade that filled us (YES terms, + = we were on the good side):", np.round(np.quantile(g, [.05, .1, .25, .5, .75, .9]), 3), " < -10c:", int((g < -0.10).sum()), "of", len(g))
