import json, sys, datetime as dt
d = json.load(open(sys.argv[1]))
def unit(x):  # per-share result, size-neutral
    return (1 - x["price"]) if x["won"] else -x["price"]
def row(name, xs):
    if not xs: print(f"{name:42s} 0"); return
    n=len(xs); l=sum(not x["won"] for x in xs)
    print(f"{name:42s} n={n:3d} lost={l:2d} ({100*l/n:4.1f}%) avg_price={sum(x['price'] for x in xs)/n:.3f} per-share sum={sum(unit(x) for x in xs):+.2f} real pnl={sum(x['pnl'] for x in xs):+.2f}")
uni = {}
for x in sorted(d, key=lambda x: (x["bot"] != "live", x["t"])):
    uni.setdefault(x["key"], x)
sets = {"LIVE": [x for x in d if x["bot"]=="live"], "TWIN-PB": [x for x in d if x["bot"]=="tier-pb"], "TWIN-RTDS": [x for x in d if x["bot"]=="tier"], "UNIQUE WINDOWS": list(uni.values())}
for nm, xs in sets.items():
    print("=====", nm); row("all", xs)
    row("p>=0.97", [x for x in xs if x["p"]>=0.97]); row("p<0.97", [x for x in xs if x["p"]<0.97])
    row("blocked earlier in window", [x for x in xs if x["nblocks"]>0]); row("never blocked", [x for x in xs if x["nblocks"]==0])
    for s in (2,5,10):
        row(f"  block within {s}s before entry", [x for x in xs if x["since_block"] is not None and x["since_block"]<=s])
    for dr in (0.5,1,1.5,2):
        row(f"dist dropped >= {dr} bp since window start", [x for x in xs if x["drop"]>=dr]); 
    row("dist drop < 1", [x for x in xs if x["drop"]<1])
    row("adv3 >= 1.0", [x for x in xs if x["adv3"]>=1.0]); row("adv3 >= 0.5", [x for x in xs if x["adv3"]>=0.5]); row("adv3 < 0.5", [x for x in xs if x["adv3"]<0.5])
    row("shock >= 0.8 lim", [x for x in xs if x["shock"] and x["shock_lim"] and x["shock"]>=0.8*x["shock_lim"]])
    row("shock < 0.8 lim", [x for x in xs if not(x["shock"] and x["shock_lim"] and x["shock"]>=0.8*x["shock_lim"])])
    row("rel < 250", [x for x in xs if x["rel"]<250]); row("rel 250-270", [x for x in xs if 250<=x["rel"]<270]); row("rel >= 270", [x for x in xs if x["rel"]>=270])
print("===== LIVE losses")
for x in sets["LIVE"]:
    if not x["won"]:
        print(dt.datetime.utcfromtimestamp(x["t"]).strftime("%d %H:%M"), x["key"][:3], "rel",x["rel"],"p",round(x["p"],3),"price",x["price"],"sh",x["shares"],"pnl",x["pnl"],"adv1",round(x["adv1"],2),"adv3",round(x["adv3"],2),"shock",x["shock"],x["shock_lim"],"since_block",x["since_block"],"nb",x["nblocks"],"drop",x["drop"],"hedged",x["hedged"])
